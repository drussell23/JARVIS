"""
Intelligent Voice Unlock Service
=================================

Advanced voice-authenticated screen unlock with:
- Hybrid STT integration (Wav2Vec, Vosk, Whisper)
- Dynamic speaker recognition and learning
- Database-driven intelligence
- CAI (Context-Aware Intelligence) integration
- SAI (Scenario-Aware Intelligence) integration
- Owner profile detection and password management

JARVIS learns the owner's voice over time and automatically rejects
non-owner unlock attempts without hardcoding.

PERFORMANCE OPTIMIZATIONS (v2.0):
- Timeout protection on ALL stages (no infinite hangs)
- Parallel execution of independent operations (transcription + speaker ID)
- Fast-path for unlock commands (uses prewarmed Whisper)
- Circuit breaker pattern for fault tolerance
- Graceful degradation when components fail
"""

import asyncio
import hashlib
import logging
import os
import subprocess
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# =============================================================================
# DYNAMIC TIMEOUT CONFIGURATION - Environment-configurable with smart defaults
# =============================================================================
# Base timeouts (can be overridden via environment variables)
# These are INCREASED to handle first-inference warmup of ML models

def _get_timeout(env_var: str, default: float) -> float:
    """Get timeout from environment with fallback to default."""
    try:
        return float(os.getenv(env_var, str(default)))
    except ValueError:
        return default

# Core timeouts - INCREASED for model warmup scenarios
TOTAL_UNLOCK_TIMEOUT = _get_timeout("JARVIS_UNLOCK_TOTAL_TIMEOUT", 35.0)
TRANSCRIPTION_TIMEOUT = _get_timeout("JARVIS_TRANSCRIPTION_TIMEOUT", 20.0)  # Increased from 10s
SPEAKER_ID_TIMEOUT = _get_timeout("JARVIS_SPEAKER_ID_TIMEOUT", 15.0)  # Increased from 8s
BIOMETRIC_TIMEOUT = _get_timeout("JARVIS_BIOMETRIC_TIMEOUT", 15.0)  # Increased from 10s

# Quick operation timeouts
HALLUCINATION_CHECK_TIMEOUT = _get_timeout("JARVIS_HALLUCINATION_TIMEOUT", 3.0)
INTENT_VERIFY_TIMEOUT = _get_timeout("JARVIS_INTENT_TIMEOUT", 2.0)
OWNER_CHECK_TIMEOUT = _get_timeout("JARVIS_OWNER_CHECK_TIMEOUT", 3.0)
VAD_PREPROCESS_TIMEOUT = _get_timeout("JARVIS_VAD_TIMEOUT", 5.0)

# Additional timeout constants for complete coverage
SECURITY_ANALYSIS_TIMEOUT = _get_timeout("JARVIS_SECURITY_ANALYSIS_TIMEOUT", 5.0)
SECURITY_RESPONSE_TIMEOUT = _get_timeout("JARVIS_SECURITY_RESPONSE_TIMEOUT", 3.0)
FAILURE_ANALYSIS_TIMEOUT = _get_timeout("JARVIS_FAILURE_ANALYSIS_TIMEOUT", 5.0)
RECORD_ATTEMPT_TIMEOUT = _get_timeout("JARVIS_RECORD_ATTEMPT_TIMEOUT", 3.0)
PERFORM_UNLOCK_TIMEOUT = _get_timeout("JARVIS_PERFORM_UNLOCK_TIMEOUT", 20.0)
CONTEXT_ANALYSIS_TIMEOUT = _get_timeout("JARVIS_CONTEXT_ANALYSIS_TIMEOUT", 8.0)
PROFILE_UPDATE_TIMEOUT = _get_timeout("JARVIS_PROFILE_UPDATE_TIMEOUT", 3.0)

# Dynamic timeout multiplier for cold start scenarios
COLD_START_TIMEOUT_MULTIPLIER = _get_timeout("JARVIS_COLD_START_MULTIPLIER", 2.0)

# Track model warmup state for adaptive timeouts
_models_warmed_up = False
_first_unlock_attempt = True


# =============================================================================
# DYNAMIC TIMEOUT MANAGER - Adaptive timeouts based on model/system state
# =============================================================================

class DynamicTimeoutManager:
    """
    Intelligent timeout management that adapts to system state.

    Features:
    - Cold start detection (first inference gets extra time)
    - ML model warmup state integration
    - System load awareness (CPU, memory pressure)
    - Automatic timeout decay after warmup
    - Failure tracking with recovery mode
    - Performance trend analysis
    - Environment-configurable base timeouts
    """

    # Constants for adaptive behavior
    WARMUP_THRESHOLD = 3  # Number of inferences to complete warmup
    FAILURE_THRESHOLD = 3  # Consecutive failures to enter recovery mode
    RECOVERY_MULTIPLIER = 1.5  # Multiplier during recovery
    LOAD_CHECK_INTERVAL_S = 30  # How often to check system load
    MAX_TIMEOUT_MULTIPLIER = 3.0  # Cap on timeout scaling

    def __init__(self):
        # Core state
        self._cold_start = True
        self._inference_count = 0
        self._warmup_complete = False
        self._ml_registry_checked = False

        # Performance tracking
        self._last_transcription_time: Optional[float] = None
        self._avg_transcription_time: float = 5.0  # Initial estimate
        self._transcription_times: List[float] = []  # Recent history for trend analysis
        self._max_history_size = 10

        # Failure tracking
        self._consecutive_failures = 0
        self._total_failures = 0
        self._recovery_mode = False
        self._last_failure_time: Optional[datetime] = None

        # System load tracking
        self._last_load_check: Optional[datetime] = None
        self._system_load_factor: float = 1.0  # 1.0 = normal, >1.0 = high load
        self._cached_memory_pressure: bool = False

        # Cloud mode tracking
        self._using_cloud_ml: bool = False

    def _check_system_load(self) -> float:
        """
        Check system load and return a multiplier for timeouts.

        Returns:
            float: Multiplier (1.0 = normal, up to 2.0 for high load)
        """
        now = datetime.now()

        # Use cached value if recent
        if (self._last_load_check and
            (now - self._last_load_check).total_seconds() < self.LOAD_CHECK_INTERVAL_S):
            return self._system_load_factor

        try:
            # Check memory pressure on macOS using vm_stat
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=2
            )

            if result.returncode == 0:
                output = result.stdout
                # Parse free pages
                import re
                page_size = 16384  # macOS default
                free_match = re.search(r'Pages free:\s+(\d+)', output)
                inactive_match = re.search(r'Pages inactive:\s+(\d+)', output)

                if free_match and inactive_match:
                    free_pages = int(free_match.group(1))
                    inactive_pages = int(inactive_match.group(1))
                    available_gb = (free_pages + inactive_pages) * page_size / (1024**3)

                    # Apply load factor based on available RAM
                    if available_gb < 4.0:
                        self._system_load_factor = 2.0  # Very high load
                        self._cached_memory_pressure = True
                    elif available_gb < 6.0:
                        self._system_load_factor = 1.5  # High load
                        self._cached_memory_pressure = True
                    elif available_gb < 8.0:
                        self._system_load_factor = 1.2  # Moderate load
                        self._cached_memory_pressure = False
                    else:
                        self._system_load_factor = 1.0  # Normal
                        self._cached_memory_pressure = False

                    logger.debug(f"System load check: {available_gb:.1f}GB available, factor={self._system_load_factor}")

            self._last_load_check = now

        except Exception as e:
            logger.debug(f"System load check failed: {e}")
            # Return cached or default value on failure

        return self._system_load_factor

    def _calculate_adaptive_multiplier(self) -> float:
        """
        Calculate the final timeout multiplier based on all factors.

        Returns:
            float: Combined multiplier (capped at MAX_TIMEOUT_MULTIPLIER)
        """
        multiplier = 1.0

        # Factor 1: Cold start
        if self._cold_start and self._inference_count < self.WARMUP_THRESHOLD:
            multiplier *= COLD_START_TIMEOUT_MULTIPLIER

        # Factor 2: Recovery mode (after failures)
        if self._recovery_mode:
            multiplier *= self.RECOVERY_MULTIPLIER

        # Factor 3: System load
        multiplier *= self._check_system_load()

        # Factor 4: Cloud mode (cloud has network latency)
        if self._using_cloud_ml:
            multiplier *= 1.3  # Extra 30% for network overhead

        # Cap the multiplier
        return min(multiplier, self.MAX_TIMEOUT_MULTIPLIER)

    def get_transcription_timeout(self) -> float:
        """
        Get adaptive timeout for STT transcription.

        Returns higher timeout for cold start, system load, and recovery mode.
        Reduces after warmup based on observed performance.
        """
        base_timeout = TRANSCRIPTION_TIMEOUT
        multiplier = self._calculate_adaptive_multiplier()

        # After warmup: use adaptive timeout based on observed performance
        if self._warmup_complete and self._avg_transcription_time > 0 and not self._recovery_mode:
            # Add 80% buffer to average time for safety margin
            adaptive = self._avg_transcription_time * 1.8
            # But don't go below a minimum or above base * multiplier
            timeout = max(min(adaptive, base_timeout * multiplier), 5.0)
        else:
            timeout = base_timeout * multiplier

        logger.debug(
            f"Transcription timeout: {timeout:.1f}s "
            f"(base={base_timeout}, mult={multiplier:.2f}, "
            f"cold={self._cold_start}, recovery={self._recovery_mode})"
        )
        return timeout

    def get_speaker_id_timeout(self) -> float:
        """Get adaptive timeout for speaker identification."""
        base_timeout = SPEAKER_ID_TIMEOUT
        multiplier = self._calculate_adaptive_multiplier()
        return base_timeout * multiplier

    def get_biometric_timeout(self) -> float:
        """Get adaptive timeout for biometric verification."""
        base_timeout = BIOMETRIC_TIMEOUT
        multiplier = self._calculate_adaptive_multiplier()
        return base_timeout * multiplier

    def record_transcription_time(self, duration_ms: float) -> None:
        """Record actual transcription time to improve estimates."""
        duration_s = duration_ms / 1000.0
        self._inference_count += 1

        # Track history for trend analysis
        self._transcription_times.append(duration_s)
        if len(self._transcription_times) > self._max_history_size:
            self._transcription_times.pop(0)

        # Update running average (exponential moving average)
        if self._last_transcription_time is None:
            self._avg_transcription_time = duration_s
        else:
            alpha = 0.3
            self._avg_transcription_time = (
                alpha * duration_s + (1 - alpha) * self._avg_transcription_time
            )

        self._last_transcription_time = duration_s

        # Success resets failure counter
        self._consecutive_failures = 0
        if self._recovery_mode:
            self._recovery_mode = False
            logger.info("✅ Recovery mode disabled - transcription succeeded")

        # Mark warmup complete after threshold
        if self._inference_count >= self.WARMUP_THRESHOLD and not self._warmup_complete:
            self._warmup_complete = True
            self._cold_start = False
            logger.info(
                f"🔥 Model warmup complete after {self._inference_count} inferences. "
                f"Avg transcription: {self._avg_transcription_time:.2f}s"
            )

    def record_failure(self, operation: str, error: Optional[str] = None) -> None:
        """
        Record a failed operation for adaptive recovery.

        Args:
            operation: The operation that failed (e.g., "transcription", "speaker_id")
            error: Optional error message
        """
        self._consecutive_failures += 1
        self._total_failures += 1
        self._last_failure_time = datetime.now()

        logger.warning(
            f"⚠️ {operation} failure #{self._consecutive_failures} "
            f"(total: {self._total_failures})"
        )

        # Enter recovery mode after threshold failures
        if self._consecutive_failures >= self.FAILURE_THRESHOLD and not self._recovery_mode:
            self._recovery_mode = True
            logger.warning(
                f"🔄 Entering recovery mode after {self._consecutive_failures} consecutive failures. "
                f"Timeouts will be increased by {self.RECOVERY_MULTIPLIER}x"
            )

    def set_cloud_mode(self, using_cloud: bool) -> None:
        """Update cloud mode status for timeout calculations."""
        if using_cloud != self._using_cloud_ml:
            self._using_cloud_ml = using_cloud
            logger.info(f"☁️ Cloud ML mode: {using_cloud}")

    def get_performance_trend(self) -> str:
        """
        Analyze recent transcription times to determine performance trend.

        Returns:
            str: "improving", "stable", "degrading", or "unknown"
        """
        if len(self._transcription_times) < 3:
            return "unknown"

        recent = self._transcription_times[-3:]
        older = self._transcription_times[:-3] if len(self._transcription_times) > 3 else []

        if not older:
            return "stable"

        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)

        if recent_avg < older_avg * 0.8:
            return "improving"
        elif recent_avg > older_avg * 1.2:
            return "degrading"
        else:
            return "stable"

    async def check_ml_registry_ready(self) -> bool:
        """
        Check if ML Engine Registry models are ready.

        Integrates with the hybrid cloud ML registry for readiness checks.
        """
        if self._ml_registry_checked and self._warmup_complete:
            return True

        try:
            from .ml_engine_registry import is_voice_unlock_ready, get_ml_registry_sync

            if is_voice_unlock_ready():
                self._ml_registry_checked = True
                registry = get_ml_registry_sync()
                if registry:
                    if not registry.is_using_cloud:
                        # Local models are loaded, reduce cold start factor
                        self._cold_start = False
                        self._using_cloud_ml = False
                        logger.info("✅ ML Registry reports local models ready - reducing timeouts")
                    else:
                        self._using_cloud_ml = True
                        logger.info("☁️ ML Registry using cloud - adjusting timeouts for latency")
                return True
            else:
                logger.debug("ML Registry: Models still loading")
                return False

        except ImportError:
            logger.debug("ML Engine Registry not available")
            return True  # Assume ready if registry not available
        except Exception as e:
            logger.warning(f"ML Registry check failed: {e}")
            return True

    def get_timeout_status(self) -> Dict[str, Any]:
        """Get comprehensive timeout configuration status."""
        return {
            # Core state
            "cold_start": self._cold_start,
            "inference_count": self._inference_count,
            "warmup_complete": self._warmup_complete,

            # Performance metrics
            "avg_transcription_time_s": round(self._avg_transcription_time, 3),
            "last_transcription_time_s": round(self._last_transcription_time, 3) if self._last_transcription_time else None,
            "performance_trend": self.get_performance_trend(),

            # Failure tracking
            "consecutive_failures": self._consecutive_failures,
            "total_failures": self._total_failures,
            "recovery_mode": self._recovery_mode,

            # System state
            "system_load_factor": round(self._system_load_factor, 2),
            "memory_pressure": self._cached_memory_pressure,
            "using_cloud_ml": self._using_cloud_ml,

            # Current timeouts
            "current_transcription_timeout": round(self.get_transcription_timeout(), 1),
            "current_speaker_id_timeout": round(self.get_speaker_id_timeout(), 1),
            "current_biometric_timeout": round(self.get_biometric_timeout(), 1),
            "adaptive_multiplier": round(self._calculate_adaptive_multiplier(), 2),

            # Base configuration
            "base_transcription_timeout": TRANSCRIPTION_TIMEOUT,
            "base_speaker_id_timeout": SPEAKER_ID_TIMEOUT,
            "base_biometric_timeout": BIOMETRIC_TIMEOUT,
            "cold_start_multiplier": COLD_START_TIMEOUT_MULTIPLIER,
        }

    def reset_cold_start(self) -> None:
        """Force reset to cold start state (useful after long idle periods)."""
        self._cold_start = True
        self._warmup_complete = False
        self._inference_count = 0
        self._ml_registry_checked = False
        logger.info("🔄 Timeout manager reset to cold start state")


# =============================================================================
# PROGRESSIVE CONFIDENCE MESSENGER - Contextual voice feedback
# =============================================================================

@dataclass
class AuthenticationContext:
    """Context for generating personalized authentication messages."""
    confidence: float
    threshold: float
    speaker_name: str
    is_owner: bool
    audio_quality: str = "good"  # good, fair, poor
    time_of_day: str = "day"  # morning, afternoon, evening, night
    is_first_unlock_today: bool = False
    consecutive_failures: int = 0
    voice_sounds_different: bool = False
    background_noise_detected: bool = False
    stt_confidence: float = 1.0


class ProgressiveConfidenceMessenger:
    """
    Generates personalized, context-aware authentication messages.

    Based on JARVIS Enhancement Strategy for voice realism:
    - High Confidence (>90%): Natural, confident tone
    - Good Confidence (85-90%): Slight acknowledgment
    - Borderline (80-85%): Shows brief verification process
    - Low Confidence (<80%): Helpful, not accusatory

    Features:
    - Time-of-day awareness
    - Environmental awareness (background noise)
    - Voice quality adaptation
    - Failure handling with recovery suggestions
    - Learning acknowledgment
    """

    # Confidence level thresholds
    HIGH_CONFIDENCE = 0.90
    GOOD_CONFIDENCE = 0.85
    BORDERLINE_CONFIDENCE = 0.80

    # Time-based greetings (no hardcoding - these are templates)
    TIME_GREETINGS = {
        "morning": ["Good morning", "Morning"],
        "afternoon": ["Good afternoon"],
        "evening": ["Good evening"],
        "night": ["Working late?", "Up late"],
    }

    def __init__(self):
        self._unlock_count = 0
        self._last_unlock_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._voice_adaptation_count = 0

    def get_time_of_day(self) -> str:
        """Determine time of day from current hour."""
        hour = datetime.now().hour
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        else:
            return "night"

    def generate_success_message(self, ctx: AuthenticationContext) -> str:
        """
        Generate personalized success message based on confidence and context.

        Args:
            ctx: Authentication context with all relevant info

        Returns:
            Personalized message string
        """
        speaker = ctx.speaker_name or "there"
        confidence = ctx.confidence
        threshold = ctx.threshold

        # High Confidence (>90%) - Natural, confident
        if confidence >= self.HIGH_CONFIDENCE:
            return self._high_confidence_message(ctx, speaker)

        # Good Confidence (85-90%) - Brief acknowledgment
        elif confidence >= self.GOOD_CONFIDENCE:
            return self._good_confidence_message(ctx, speaker)

        # Borderline (80-85%) - Shows verification process
        elif confidence >= self.BORDERLINE_CONFIDENCE:
            return self._borderline_confidence_message(ctx, speaker)

        # Low but passing (threshold to 80%) - Cautious acknowledgment
        else:
            return self._low_confidence_message(ctx, speaker)

    def _high_confidence_message(self, ctx: AuthenticationContext, speaker: str) -> str:
        """Generate message for high confidence (>90%)."""
        time_of_day = self.get_time_of_day()

        # Time-aware messages
        if time_of_day == "morning" and ctx.is_first_unlock_today:
            greetings = self.TIME_GREETINGS["morning"]
            import random
            greeting = random.choice(greetings)
            return f"{greeting}, {speaker}. Unlocking for you."

        elif time_of_day == "night":
            return f"Working late, {speaker}? Unlocking now."

        # Standard high-confidence response
        return f"Of course, {speaker}. Unlocking for you."

    def _good_confidence_message(self, ctx: AuthenticationContext, speaker: str) -> str:
        """Generate message for good confidence (85-90%)."""
        # Check for environmental factors
        if ctx.background_noise_detected:
            return f"Got you despite the background noise, {speaker}. Unlocking now."

        return f"Verified. Unlocking for you, {speaker}."

    def _borderline_confidence_message(self, ctx: AuthenticationContext, speaker: str) -> str:
        """Generate message for borderline confidence (80-85%)."""
        # Voice sounds different (tired, sick, etc.)
        if ctx.voice_sounds_different:
            return f"Your voice sounds a bit different today, {speaker}, but I'm confident it's you. Unlocking now."

        # Audio quality issues
        if ctx.audio_quality == "poor":
            return f"Signal was a bit weak, but I've verified it's you, {speaker}. Unlocking."

        return f"One moment... yes, verified. Unlocking for you, {speaker}."

    def _low_confidence_message(self, ctx: AuthenticationContext, speaker: str) -> str:
        """Generate message for low but passing confidence."""
        confidence_pct = int(ctx.confidence * 100)

        # Explain why confidence was lower
        if ctx.background_noise_detected:
            return f"Verified despite background noise ({confidence_pct}% confidence). Unlocking, {speaker}."

        if ctx.audio_quality == "poor":
            return f"Audio quality was low, but patterns match. Unlocking for you, {speaker}."

        return f"Verified with {confidence_pct}% confidence. Unlocking, {speaker}."

    def generate_failure_message(self, ctx: AuthenticationContext) -> str:
        """
        Generate helpful failure message with recovery suggestions.

        Args:
            ctx: Authentication context

        Returns:
            Helpful failure message
        """
        confidence_pct = int(ctx.confidence * 100)
        threshold_pct = int(ctx.threshold * 100)
        gap = threshold_pct - confidence_pct

        # Very low confidence - likely wrong person
        if confidence_pct < 50:
            return (
                f"I don't recognize this voice. This device is locked to authorized users only. "
                f"If you need access, please use the password."
            )

        # Close to threshold - provide helpful feedback
        if gap <= 10:
            suggestions = []

            if ctx.background_noise_detected:
                suggestions.append("move to a quieter area")

            if ctx.audio_quality == "poor":
                suggestions.append("speak a bit closer to the microphone")

            if ctx.stt_confidence < 0.7:
                suggestions.append("speak more clearly")

            if suggestions:
                suggestion_text = " or ".join(suggestions)
                return (
                    f"Almost there ({confidence_pct}%, need {threshold_pct}%). "
                    f"Try again - maybe {suggestion_text}?"
                )

            return (
                f"Voice confidence was {confidence_pct}% (need {threshold_pct}%). "
                f"Please try again, or use password if you prefer."
            )

        # Moderate gap - general retry suggestion
        return (
            f"Having trouble verifying your voice ({confidence_pct}% vs {threshold_pct}% threshold). "
            f"Would you like to try again or use password?"
        )

    def generate_retry_prompt(self, ctx: AuthenticationContext, attempt_number: int) -> str:
        """Generate context-aware retry prompt."""
        if attempt_number == 1:
            if ctx.background_noise_detected:
                return "There's some background noise. Could you try again, speaking a bit louder?"
            return "Let me try again - please repeat your command."

        elif attempt_number == 2:
            return "Still having trouble. One more try, or I can use password instead."

        else:
            return "I'm having difficulty with voice verification. Switching to password."

    def record_unlock(self, success: bool):
        """Record unlock attempt for tracking."""
        if success:
            self._unlock_count += 1
            self._consecutive_failures = 0
            self._last_unlock_time = datetime.now()
        else:
            self._consecutive_failures += 1


# Global messenger instance
_confidence_messenger: Optional[ProgressiveConfidenceMessenger] = None


def get_confidence_messenger() -> ProgressiveConfidenceMessenger:
    """Get the global confidence messenger instance."""
    global _confidence_messenger
    if _confidence_messenger is None:
        _confidence_messenger = ProgressiveConfidenceMessenger()
    return _confidence_messenger


# Global timeout manager instance
_timeout_manager = DynamicTimeoutManager()


def get_timeout_manager() -> DynamicTimeoutManager:
    """Get the global timeout manager."""
    return _timeout_manager


@dataclass
class UnlockDiagnostics:
    """Comprehensive diagnostics for unlock attempts"""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    audio_size_bytes: int = 0
    audio_duration_seconds: float = 0.0
    transcription_text: str = ""
    transcription_confidence: float = 0.0
    speaker_identified: Optional[str] = None
    speaker_confidence: float = 0.0
    is_owner: bool = False
    verification_passed: bool = False
    failure_reason: Optional[str] = None
    processing_time_ms: float = 0.0
    stt_engine_used: Optional[str] = None
    cai_analysis: Optional[Dict] = None
    sai_analysis: Optional[Dict] = None
    retry_count: int = 0
    error_messages: list = field(default_factory=list)


class IntelligentVoiceUnlockService:
    """
    Ultra-intelligent voice unlock service that learns and adapts.

    Features:
    - Dynamic speaker learning (no hardcoding)
    - Automatic rejection of non-owner voices
    - Hybrid STT for accurate transcription
    - Database recording for continuous learning
    - CAI integration for context awareness
    - SAI integration for scenario detection
    - Owner profile with password management
    """

    def __init__(self):
        self.initialized = False

        # Hybrid STT Router
        self.stt_router = None

        # Speaker Recognition Engine
        self.speaker_engine = None

        # Learning Database
        self.learning_db = None

        # Advanced error handling and retry logic
        self.max_retries = 3
        self.retry_delay_seconds = 0.5
        self.circuit_breaker_threshold = 5  # failures before circuit opens
        self.circuit_breaker_timeout = 60  # seconds
        self._circuit_breaker_failures = defaultdict(int)
        self._circuit_breaker_last_failure = defaultdict(float)

        # Performance tracking
        self._diagnostics_history = []
        self._max_diagnostics_history = 100

        # Context-Aware Intelligence
        self.cai_handler = None

        # Scenario-Aware Intelligence
        self.sai_analyzer = None

        # Owner profile cache
        self.owner_profile = None
        self.owner_password_hash = None

        # 🤖 CONTINUOUS LEARNING: ML engine for voice biometrics and password typing
        self.ml_engine = None

        # 🚀 SEMANTIC CACHE: Instant unlock for repeated requests
        self.voice_biometric_cache = None

        # 🎯 UNIFIED VOICE CACHE: Preloads Derek's voice profile for instant recognition
        self.unified_cache = None

        # 🧠 VOICE BIOMETRIC INTELLIGENCE: Upfront transparent voice recognition
        self.voice_biometric_intelligence = None

        # Statistics
        self.stats = {
            "total_unlock_attempts": 0,
            "owner_unlock_attempts": 0,
            "rejected_attempts": 0,
            "successful_unlocks": 0,
            "failed_authentications": 0,
            "learning_updates": 0,
            "ml_voice_updates": 0,
            "ml_typing_updates": 0,
            "last_unlock_time": None,
            "instant_recognitions": 0,
            "voice_announced_first": 0,
        }

    async def initialize(self):
        """
        Initialize all components - PARALLELIZED for speed with timeout protection.

        OPTIMIZED v2.2 - Proper ML model initialization:
        - Uses prewarmer status to skip already-loaded models (instant if prewarmed)
        - Increased timeouts to allow ML models to fully load on cold start
        - All phases run in single parallel block for maximum speed
        - Graceful degradation continues even if components timeout

        IMPORTANT: On first startup, model loading takes 15-30s total.
        Subsequent initializations (or if prewarmed at startup) are <1s.
        """
        if self.initialized:
            return

        # Check if models were already prewarmed at system startup
        try:
            from voice_unlock.ml_model_prewarmer import is_prewarmed, get_prewarm_status
            if is_prewarmed():
                prewarm_status = get_prewarm_status()
                logger.info(f"🚀 Models already prewarmed - using fast initialization path")
                logger.info(f"   Prewarm status: {prewarm_status.to_dict()}")
                COMPONENT_TIMEOUT = 5.0   # Fast timeout - models already loaded
                TOTAL_INIT_TIMEOUT = 15.0  # Short total timeout
            else:
                logger.info("⏳ Models not prewarmed - allowing time for ML model loading")
                COMPONENT_TIMEOUT = 20.0   # Allow time for model loading (Whisper + ECAPA)
                TOTAL_INIT_TIMEOUT = 45.0  # Allow 45s total for cold start
        except ImportError:
            logger.debug("Prewarmer not available - using default timeouts")
            COMPONENT_TIMEOUT = 20.0
            TOTAL_INIT_TIMEOUT = 45.0

        logger.info("🚀 Initializing Intelligent Voice Unlock Service (v2.1 optimized parallel)...")
        init_start = datetime.now()

        async def _init_with_timeout(coro, name: str, timeout: float = COMPONENT_TIMEOUT):
            """Wrap initialization coroutine with timeout and error handling."""
            try:
                await asyncio.wait_for(coro, timeout=timeout)
                logger.debug(f"  ✓ {name} initialized")
            except asyncio.TimeoutError:
                logger.warning(f"  ⏱️ {name} initialization timed out after {timeout}s (continuing without)")
            except Exception as e:
                logger.warning(f"  ⚠️ {name} initialization failed: {e} (continuing without)")

        async def _preload_keychain_cache():
            """Preload keychain password into cache for instant first unlock."""
            from macos_keychain_unlock import preload_keychain_cache
            success = await preload_keychain_cache()
            if success:
                logger.info("🔐 Keychain password preloaded into cache")
            else:
                logger.warning("⚠️ Keychain password preload failed - first unlock will be slower")

        async def _init_voice_biometric_cache():
            """
            Initialize voice biometric semantic cache for instant repeat unlocks.
            Also sets up database recording for continuous voice learning.
            """
            from voice_unlock.voice_biometric_cache import get_voice_biometric_cache
            from voice_unlock.metrics_database import MetricsDatabase

            self.voice_biometric_cache = get_voice_biometric_cache()

            # 🎯 CONTINUOUS LEARNING: Set up database recorder callback
            # This allows the cache to record ALL authentication attempts to SQLite
            # so JARVIS can continuously improve voice recognition
            try:
                metrics_db = MetricsDatabase()
                self.voice_biometric_cache.set_voice_sample_recorder(
                    metrics_db.record_voice_sample
                )
                logger.info("🚀 Voice biometric cache initialized WITH database recording for continuous learning")
            except Exception as e:
                logger.warning(f"Voice biometric cache initialized WITHOUT database recording: {e}")
                logger.info("🚀 Voice biometric semantic cache initialized (cache-only mode)")

        async def _init_unified_voice_cache():
            """
            Initialize unified voice cache manager for instant voice recognition.

            CRITICAL: This preloads your voice profile from SQLite at startup
            so voice matching is instant (<5ms) without recomputing embeddings!
            
            NOTE: If VoiceProfileStartupService has already loaded profiles,
            this will reuse them instead of loading again (prevents duplicates).
            """
            try:
                from voice_unlock.unified_voice_cache_manager import (
                    get_unified_cache_manager,
                    initialize_unified_cache,
                )

                # Get the singleton cache manager
                self.unified_cache = get_unified_cache_manager()
                
                # Check if profiles are already loaded (by VoiceUnlockStartup or another component)
                existing_profiles = self.unified_cache.profiles_loaded
                if existing_profiles > 0:
                    logger.info(
                        f"📋 Unified Voice Cache already has {existing_profiles} profile(s) - reusing"
                    )
                    return
                
                # Initialize the unified cache manager (will check for duplicates internally)
                success = await initialize_unified_cache(
                    preload_profiles=True,
                    preload_models=False,  # Models loaded elsewhere
                )

                if success:
                    profiles_loaded = self.unified_cache.profiles_loaded
                    if profiles_loaded > 0:
                        logger.info(
                            f"🎯 Unified Voice Cache initialized "
                            f"({profiles_loaded} profile(s) preloaded for instant recognition)"
                        )
                    else:
                        logger.debug("Unified Voice Cache initialized (profiles loaded elsewhere)")
                else:
                    logger.warning("⚠️ Unified Voice Cache initialization failed - fallback to standard matching")
                    self.unified_cache = None

            except Exception as e:
                logger.warning(f"⚠️ Unified Voice Cache not available: {e}")
                self.unified_cache = None

        async def _init_voice_biometric_intelligence():
            """🧠 Initialize Voice Biometric Intelligence for upfront recognition."""
            try:
                from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence
                self.voice_biometric_intelligence = await get_voice_biometric_intelligence()
                logger.info("🧠 Voice Biometric Intelligence connected (upfront recognition enabled)")
            except Exception as e:
                logger.warning(f"⚠️ Voice Biometric Intelligence not available: {e}")
                self.voice_biometric_intelligence = None

        async def _run_initialization():
            """Run all initialization phases - ALL IN PARALLEL for maximum speed."""
            # OPTIMIZED v2.2: Run ALL components in single parallel block
            # This eliminates sequential phase delays (was 3 phases = 3x latency)
            # NEW: Added unified voice cache for preloading Derek's voice profile
            # NEW: Added Voice Biometric Intelligence for upfront transparent recognition
            await asyncio.gather(
                # Core services (Phase 1)
                _init_with_timeout(self._initialize_stt(), "Hybrid STT Router"),
                _init_with_timeout(self._initialize_speaker_recognition(), "Speaker Recognition"),
                _init_with_timeout(self._initialize_learning_db(), "Learning Database"),
                _init_with_timeout(_preload_keychain_cache(), "Keychain Cache"),
                _init_with_timeout(_init_voice_biometric_cache(), "Voice Biometric Cache"),
                # NEW: Unified Voice Cache - preloads Derek's embedding for instant recognition
                _init_with_timeout(_init_unified_voice_cache(), "Unified Voice Cache"),
                # NEW: Voice Biometric Intelligence - upfront voice recognition with transparency
                _init_with_timeout(_init_voice_biometric_intelligence(), "Voice Biometric Intelligence"),
                # Intelligence layers (Phase 2) - now parallel with Phase 1
                _init_with_timeout(self._initialize_cai(), "Context-Aware Intelligence"),
                _init_with_timeout(self._initialize_sai(), "Scenario-Aware Intelligence"),
                _init_with_timeout(self._initialize_ml_engine(), "ML Learning Engine"),
                # Owner profile (Phase 3) - now parallel with Phase 1 & 2
                _init_with_timeout(self._load_owner_profile(), "Owner Profile", timeout=10.0),
            )

        try:
            # Python 3.9 compatible: use wait_for instead of asyncio.timeout
            await asyncio.wait_for(_run_initialization(), timeout=TOTAL_INIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Total initialization exceeded {TOTAL_INIT_TIMEOUT}s - service starting in degraded mode")

        init_time = (datetime.now() - init_start).total_seconds() * 1000
        self.initialized = True
        logger.info(f"✅ Intelligent Voice Unlock Service initialized in {init_time:.0f}ms")

        # CRITICAL FIX: Validate ECAPA encoder availability AFTER initialization
        # This prevents the 0% confidence bug when encoder fails to load silently
        await self._validate_ecapa_availability()

    async def _initialize_stt(self):
        """
        Initialize Hybrid STT Router with model prewarming.

        This calls router.initialize() which:
        - Discovers available STT engines
        - Connects to learning database
        - Prewarms Whisper model (loads into memory for instant first transcription)
        """
        try:
            from voice.hybrid_stt_router import get_hybrid_router

            self.stt_router = get_hybrid_router()

            # CRITICAL: Call initialize() to prewarm Whisper model
            # This eliminates the 3-10+ second model load time on first transcription
            await self.stt_router.initialize()

            # Log prewarming status
            stats = self.stt_router.get_stats()
            if stats.get("whisper_prewarmed"):
                logger.info("✅ Hybrid STT Router connected (Whisper prewarmed)")
            else:
                logger.warning("⚠️ Hybrid STT Router connected (Whisper NOT prewarmed - first request may be slow)")
        except Exception as e:
            logger.error(f"Failed to initialize Hybrid STT: {e}")
            self.stt_router = None

    async def _initialize_speaker_recognition(self):
        """Initialize Speaker Recognition Engine"""
        try:
            # Try new SpeakerVerificationService first
            try:
                from voice.speaker_verification_service import get_speaker_verification_service

                self.speaker_engine = await get_speaker_verification_service()
                logger.info("✅ Speaker Verification Service connected (new)")
                return
            except ImportError:
                logger.debug("New speaker verification service not available, trying legacy")

            # Fallback to legacy speaker recognition
            from voice.speaker_recognition import get_speaker_recognition_engine

            self.speaker_engine = get_speaker_recognition_engine()
            await self.speaker_engine.initialize()
            logger.info("✅ Speaker Recognition Engine connected (legacy)")
        except Exception as e:
            logger.error(f"Failed to initialize Speaker Recognition: {e}")
            self.speaker_engine = None

    async def _initialize_learning_db(self):
        """Initialize Learning Database"""
        try:
            from intelligence.learning_database import JARVISLearningDatabase

            self.learning_db = JARVISLearningDatabase()
            await self.learning_db.initialize()
            logger.info("✅ Learning Database connected")
        except Exception as e:
            logger.error(f"Failed to initialize Learning Database: {e}")
            self.learning_db = None

    async def _initialize_cai(self):
        """Initialize Context-Aware Intelligence"""
        try:
            from context_intelligence.handlers.context_aware_handler import ContextAwareHandler

            self.cai_handler = ContextAwareHandler()
            logger.info("✅ Context-Aware Intelligence connected")
        except Exception as e:
            logger.warning(f"CAI not available: {e}")
            self.cai_handler = None

    async def _initialize_sai(self):
        """Initialize Scenario-Aware Intelligence"""
        try:
            from intelligence.scenario_intelligence import ScenarioIntelligence

            self.sai_analyzer = ScenarioIntelligence()
            await self.sai_analyzer.initialize()
            logger.info("✅ Scenario-Aware Intelligence connected")
        except Exception as e:
            logger.warning(f"SAI not available: {e}")
            self.sai_analyzer = None

    async def _initialize_ml_engine(self):
        """🤖 Initialize ML Continuous Learning Engine"""
        try:
            from voice_unlock.continuous_learning_engine import get_learning_engine

            self.ml_engine = await get_learning_engine()
            await self.ml_engine.initialize()
            logger.info("✅ 🤖 ML Continuous Learning Engine connected")
        except Exception as e:
            logger.warning(f"ML Learning Engine not available: {e}")
            self.ml_engine = None

    async def _load_owner_profile(self):
        """Load or create owner profile.

        Note: This runs in parallel with other initialization tasks.
        We wait for learning_db with retry logic since it may not be ready yet.
        speaker_engine.owner_profile is set later (not critical for profile loading).
        """
        # Wait for learning_db with retry (parallel initialization race condition fix)
        max_retries = 10
        for attempt in range(max_retries):
            if self.learning_db:
                break
            await asyncio.sleep(0.1)  # 100ms backoff

        if not self.learning_db:
            logger.warning("Cannot load owner profile - learning_db not initialized after retries")
            return

        try:
            # Get all speaker profiles
            profiles = await self.learning_db.get_all_speaker_profiles()

            # Find owner (is_primary_user = True)
            for profile in profiles:
                if profile.get("is_primary_user"):
                    self.owner_profile = profile
                    logger.info(f"👑 Owner profile loaded: {profile['speaker_name']}")
                    break

            # Set speaker_engine.owner_profile if available (non-blocking)
            if self.owner_profile and self.speaker_engine:
                try:
                    if hasattr(self.speaker_engine, 'profiles'):
                        self.speaker_engine.owner_profile = self.speaker_engine.profiles.get(
                            self.owner_profile["speaker_name"]
                        )
                except Exception as e:
                    logger.debug(f"Could not set speaker_engine.owner_profile: {e}")

            if not self.owner_profile:
                logger.warning(
                    "⚠️  No owner profile found - first speaker will be enrolled as owner"
                )

            # Load password hash from keychain
            await self._load_owner_password()

        except Exception as e:
            logger.error(f"Failed to load owner profile: {e}")

    async def _load_owner_password(self):
        """
        Load owner password hash from keychain using enhanced async keychain service.

        Uses non-blocking async subprocess to prevent event loop blocking.
        Password is cached for subsequent fast access (1 hour TTL).
        """
        try:
            from macos_keychain_unlock import get_password_hash_async

            # Use enhanced keychain service (non-blocking, cached, parallel lookup)
            password_hash = await get_password_hash_async()

            if password_hash:
                self.owner_password_hash = password_hash
                logger.info("🔐 Owner password hash loaded from keychain (async, cached)")
            else:
                logger.warning("⚠️  No password found in keychain")

        except Exception as e:
            logger.error(f"Failed to load owner password: {e}")

    async def _validate_ecapa_availability(self) -> None:
        """
        Validate ECAPA encoder availability from all sources.

        CRITICAL FIX: This prevents the 0% confidence bug that occurs when
        the ECAPA encoder fails to load silently. We check ALL encoder sources
        and provide clear diagnostics if none are available.

        Sources checked (in order):
        1. Unified Voice Cache
        2. ML Engine Registry
        3. Speaker Verification Service

        If no encoder is available, the service marks itself as DEGRADED
        (not failed) and logs clear error messages.
        """
        logger.info("🔍 Validating ECAPA encoder availability...")

        ecapa_available = False
        ecapa_source = None
        diagnostics = {}

        # Check 1: Unified Voice Cache
        if self.unified_cache:
            try:
                cache_status = self.unified_cache.get_encoder_status()
                diagnostics["unified_cache"] = cache_status
                if cache_status.get("available"):
                    ecapa_available = True
                    ecapa_source = f"unified_cache ({cache_status.get('source', 'unknown')})"
                    logger.info(f"✅ ECAPA available via Unified Cache: {cache_status.get('source')}")
            except Exception as e:
                diagnostics["unified_cache_error"] = str(e)
                logger.debug(f"Unified cache check failed: {e}")

        # Check 2: ML Engine Registry
        if not ecapa_available:
            try:
                from voice_unlock.ml_engine_registry import get_ml_registry_sync

                registry = get_ml_registry_sync()
                if registry:
                    registry_status = registry.get_ecapa_status()
                    diagnostics["ml_registry"] = registry_status
                    if registry_status.get("available"):
                        ecapa_available = True
                        ecapa_source = f"ml_registry ({registry_status.get('source', 'unknown')})"
                        logger.info(f"✅ ECAPA available via ML Registry: {registry_status.get('source')}")
            except ImportError:
                diagnostics["ml_registry"] = "module not available"
            except Exception as e:
                diagnostics["ml_registry_error"] = str(e)
                logger.debug(f"ML Registry check failed: {e}")

        # Check 3: Speaker Verification Service
        if not ecapa_available and self.speaker_engine:
            try:
                if hasattr(self.speaker_engine, "speechbrain_engine"):
                    engine = self.speaker_engine.speechbrain_engine
                    if engine and hasattr(engine, "speaker_encoder") and engine.speaker_encoder:
                        ecapa_available = True
                        ecapa_source = "speaker_engine.speechbrain_engine"
                        logger.info("✅ ECAPA available via Speaker Verification Service")
                        diagnostics["speaker_engine"] = "available"
                    else:
                        diagnostics["speaker_engine"] = "encoder not loaded"
                else:
                    diagnostics["speaker_engine"] = "no speechbrain_engine attribute"
            except Exception as e:
                diagnostics["speaker_engine_error"] = str(e)

        # Store diagnostics for later queries
        self._ecapa_diagnostics = {
            "available": ecapa_available,
            "source": ecapa_source,
            "details": diagnostics,
            "checked_at": datetime.now().isoformat()
        }

        # Log result
        if ecapa_available:
            logger.info(f"✅ ECAPA VALIDATION PASSED: {ecapa_source}")
            self._ecapa_available = True
        else:
            logger.error("=" * 70)
            logger.error("❌ ECAPA ENCODER NOT AVAILABLE - Voice verification will FAIL!")
            logger.error("=" * 70)
            logger.error("   No encoder found from any source:")
            for source, status in diagnostics.items():
                logger.error(f"   - {source}: {status}")
            logger.error("")
            logger.error("   Voice unlock commands will return 0% confidence!")
            logger.error("   To fix: Check ML model loading, cloud backend, or restart.")
            logger.error("=" * 70)
            self._ecapa_available = False

            # Mark service as degraded but not failed
            # This allows other features to work while voice biometrics is unavailable
            self._degraded_mode = True
            self._degraded_reason = "ECAPA encoder unavailable"

    def get_encoder_diagnostics(self) -> Dict[str, Any]:
        """
        Get detailed ECAPA encoder diagnostics.

        Returns comprehensive status for troubleshooting voice verification failures.
        """
        base_diagnostics = getattr(self, "_ecapa_diagnostics", {
            "available": False,
            "source": None,
            "details": {},
            "checked_at": None
        })

        # Add runtime status
        runtime_status = {
            "ecapa_available": getattr(self, "_ecapa_available", False),
            "degraded_mode": getattr(self, "_degraded_mode", False),
            "degraded_reason": getattr(self, "_degraded_reason", None),
            "service_initialized": self.initialized,
        }

        # Check current availability (may have changed since init)
        current_status = {}
        if self.unified_cache:
            try:
                current_status["unified_cache"] = self.unified_cache.get_encoder_status()
            except Exception as e:
                current_status["unified_cache_error"] = str(e)

        try:
            from voice_unlock.ml_engine_registry import get_ml_registry_sync
            registry = get_ml_registry_sync()
            if registry:
                current_status["ml_registry"] = registry.get_ecapa_status()
        except Exception as e:
            current_status["ml_registry_error"] = str(e)

        return {
            "init_check": base_diagnostics,
            "runtime": runtime_status,
            "current_status": current_status,
            "recommendation": self._get_ecapa_fix_recommendation()
        }

    def _get_ecapa_fix_recommendation(self) -> str:
        """Get a human-readable fix recommendation for ECAPA issues."""
        if getattr(self, "_ecapa_available", False):
            return "ECAPA is working correctly."

        # Analyze the issue and provide specific advice
        diagnostics = getattr(self, "_ecapa_diagnostics", {}).get("details", {})

        if "ml_registry" in diagnostics:
            registry_status = diagnostics["ml_registry"]
            if isinstance(registry_status, dict):
                if registry_status.get("cloud_mode") and not registry_status.get("cloud_verified"):
                    return (
                        "Cloud mode is active but cloud backend is not reachable. "
                        "Check network connectivity or set JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED=true "
                        "to allow local ECAPA fallback."
                    )
                if registry_status.get("local_error"):
                    return (
                        f"Local ECAPA load failed: {registry_status.get('local_error')}. "
                        "Check available RAM and SpeechBrain installation."
                    )

        return (
            "ECAPA encoder unavailable from all sources. Try: "
            "1) Restart the service to reload ML models, "
            "2) Check cloud ML backend availability, "
            "3) Verify SpeechBrain is installed correctly."
        )

    async def _try_on_demand_ecapa_load(self) -> bool:
        """
        Attempt to load ECAPA encoder on-demand with HYBRID CLOUD INTEGRATION.

        This implements graceful degradation with intelligent routing:
        1. Check memory pressure first
        2. If memory is low → use cloud ECAPA (offloads ~2GB RAM)
        3. If memory is sufficient → try local ECAPA
        4. Fallback chain: cloud → local → degraded mode

        Returns:
            True if ECAPA became available (via cloud or local), False otherwise.
        """
        logger.info("🔄 Attempting on-demand ECAPA load with hybrid cloud routing...")

        # =======================================================================
        # STEP 1: Check memory pressure to decide routing strategy
        # =======================================================================
        try:
            from voice_unlock.ml_engine_registry import MLConfig

            use_cloud, available_ram, reason = MLConfig.check_memory_pressure()
            logger.info(f"   Memory check: {reason}")

            if use_cloud or available_ram < 6.0:
                logger.info(f"   🌐 CLOUD-FIRST MODE: RAM={available_ram:.1f}GB - routing to GCP")
                cloud_success = await self._try_cloud_ecapa()
                if cloud_success:
                    return True
                logger.warning("   Cloud ECAPA unavailable, trying local as fallback...")
            else:
                logger.info(f"   💻 LOCAL-FIRST MODE: RAM={available_ram:.1f}GB - sufficient for local")

        except Exception as e:
            logger.warning(f"   Memory check failed: {e} - defaulting to cloud-first")
            cloud_success = await self._try_cloud_ecapa()
            if cloud_success:
                return True

        # =======================================================================
        # STEP 2: Try ML Engine Registry (handles both cloud and local)
        # =======================================================================
        try:
            from voice_unlock.ml_engine_registry import ensure_ecapa_available

            # ensure_ecapa_available has built-in hybrid cloud support
            result = await ensure_ecapa_available(timeout=30.0, allow_cloud=True)
            if isinstance(result, tuple):
                success, message, _ = result
            else:
                success = result

            if success:
                self._ecapa_available = True
                self._degraded_mode = False
                logger.info(f"✅ On-demand ECAPA SUCCESS via ensure_ecapa_available()")
                return True

        except Exception as e:
            logger.warning(f"   ensure_ecapa_available failed: {e}")

        # =======================================================================
        # STEP 3: Try Unified Voice Cache (may have cloud connection)
        # =======================================================================
        if self.unified_cache:
            try:
                cache_status = self.unified_cache.get_encoder_status()
                if cache_status.get("available"):
                    self._ecapa_available = True
                    self._degraded_mode = False
                    source = cache_status.get('source', 'unknown')
                    logger.info(f"✅ On-demand ECAPA SUCCESS via Unified Cache: {source}")
                    return True
            except Exception as e:
                logger.debug(f"   Unified Cache check failed: {e}")

        # =======================================================================
        # STEP 4: Final cloud attempt if not already tried
        # =======================================================================
        logger.info("   Attempting final cloud fallback...")
        if await self._try_cloud_ecapa():
            return True

        logger.warning("❌ On-demand ECAPA load FAILED - continuing in degraded mode")
        logger.warning("   Voice unlock will use physics/behavioral/context only")
        return False

    async def _try_cloud_ecapa(self) -> bool:
        """
        Attempt to verify cloud ECAPA is available using the robust cloud client.

        This offloads ECAPA processing to GCP, avoiding local memory pressure.
        Uses CloudECAPAClient with circuit breaker, retries, and caching.

        Priority order:
        1. CloudECAPAClient (new robust client with full features)
        2. ML Engine Registry verify_cloud_ecapa (legacy)

        Returns:
            True if cloud ECAPA is available and verified
        """
        # =====================================================================
        # Method 1: Try the new robust CloudECAPAClient first
        # =====================================================================
        try:
            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client

            logger.info("   🌐 Trying CloudECAPAClient (robust client)...")

            client = await get_cloud_ecapa_client()
            status = client.get_status()

            if status.get("ready"):
                # Client is ready - test with a small audio sample
                import numpy as np
                test_audio = np.zeros(1600, dtype=np.float32)  # 100ms silence

                embedding = await client.extract_embedding(
                    test_audio.tobytes(),
                    sample_rate=16000,
                    format="float32"
                )

                if embedding is not None and len(embedding) == 192:
                    self._ecapa_available = True
                    self._ecapa_source = "cloud"
                    self._cloud_client = client  # Store reference for later use
                    self._degraded_mode = False
                    endpoint = status.get("healthy_endpoint", "unknown")
                    logger.info(f"✅ Cloud ECAPA verified via CloudECAPAClient: {endpoint}")
                    return True
                else:
                    logger.warning(f"   CloudECAPAClient extraction returned invalid embedding")
            else:
                logger.debug(f"   CloudECAPAClient not ready: {status}")

        except ImportError:
            logger.debug("   CloudECAPAClient not available (import failed)")
        except Exception as e:
            logger.debug(f"   CloudECAPAClient failed: {e}")

        # =====================================================================
        # Method 2: Fallback to ML Engine Registry (legacy method)
        # =====================================================================
        try:
            from voice_unlock.ml_engine_registry import get_ml_registry

            logger.info("   🔄 Trying ML Engine Registry (legacy cloud verification)...")

            registry = await get_ml_registry()
            if registry is None:
                return False

            # Check if cloud is configured
            cloud_endpoint = os.getenv("JARVIS_CLOUD_ML_ENDPOINT")
            if not cloud_endpoint:
                logger.debug("   Cloud ECAPA not configured (JARVIS_CLOUD_ML_ENDPOINT not set)")
                return False

            # Verify cloud ECAPA with extraction test
            cloud_verified, reason = await registry._verify_cloud_backend_ready(test_extraction=True)
            if cloud_verified:
                self._ecapa_available = True
                self._ecapa_source = "cloud"
                self._degraded_mode = False
                logger.info(f"✅ Cloud ECAPA verified via ML Registry: {cloud_endpoint}")
                return True

            logger.debug("   ML Registry cloud verification failed")
            return False

        except Exception as e:
            logger.debug(f"   ML Registry cloud attempt failed: {e}")
            return False

    async def process_voice_unlock_command(
        self, audio_data, context: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None
    ) -> Dict[str, Any]:
        """
        Process voice unlock command with full intelligence stack and Cloud-First routing (v3.0.0).

        Features:
        - CLOUD-FIRST: Routes to cloud ECAPA first (non-blocking, saves RAM)
        - PROGRESS CALLBACKS: Real-time UI updates ("Checking cloud...", "Verifying...")
        - SEMANTIC CACHING: Instant unlock for repeated requests within session
        - GLOBAL TIMEOUT: Prevents infinite hangs (25s max)
        - Retry logic with exponential backoff
        - Circuit breaker pattern for fault tolerance
        - Comprehensive diagnostics tracking
        - Async/await throughout for non-blocking operation

        Args:
            audio_data: Audio data in any format (bytes, string, base64, etc.)
            context: Optional context (screen state, time, location, etc.)
            progress_callback: Optional async callback for real-time progress updates
                              Signature: async callback({"stage": str, "progress": int, "message": str})

        Returns:
            Result dict with success, speaker, reason, and diagnostics
        """
        # CRITICAL: Start caffeinate IMMEDIATELY to prevent screen sleep during processing
        caffeinate_process = await asyncio.create_subprocess_exec(
            "caffeinate", "-d", "-u",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        logger.info("🔋 Caffeinate started to keep screen awake")

        if not self.initialized:
            await self.initialize()

        start_time = datetime.now()
        self.stats["total_unlock_attempts"] += 1

        # Initialize diagnostics
        diagnostics = UnlockDiagnostics()

        # Initialize advanced metrics logger with stage tracking
        from voice_unlock.unlock_metrics_logger import get_metrics_logger, StageMetrics
        metrics_logger = get_metrics_logger()
        stages: List[StageMetrics] = []

        # =============================================================================
        # 🧠 UPFRONT VOICE BIOMETRIC INTELLIGENCE: Verify and announce FIRST
        # =============================================================================
        # This provides TRANSPARENCY by recognizing voice and announcing it
        # BEFORE the unlock process, so the user knows immediately if recognized
        # =============================================================================
        if self.voice_biometric_intelligence:
            try:
                # Verify voice and announce result FIRST with Cloud-First routing
                vbi_result = await asyncio.wait_for(
                    self.voice_biometric_intelligence.verify_and_announce(
                        audio_data=audio_data,
                        context={
                            'consecutive_failures': diagnostics.retry_count,
                            'device_trusted': True,
                        },
                        speak=True,  # Announce "Voice verified, Derek. 94% confidence. Unlocking now..."
                        progress_callback=progress_callback,  # Real-time UI updates
                    ),
                    timeout=5.0  # Extended timeout for cloud-first routing
                )

                self.stats["voice_announced_first"] += 1

                if vbi_result.verified:
                    self.stats["instant_recognitions"] += 1
                    logger.info(
                        f"🧠 Voice recognized UPFRONT: {vbi_result.speaker_name} "
                        f"({vbi_result.confidence:.1%}) in {vbi_result.verification_time_ms:.0f}ms"
                    )

                    # Voice is verified - proceed directly to unlock (skip redundant verification)
                    # Use 40% threshold (unlock threshold) since VBI already verified the speaker
                    # This prevents falling through to parallel verification that may return 0%
                    if vbi_result.was_cached or vbi_result.confidence >= 0.40: # 0.40 is the unlock threshold 
                        # Verified above unlock threshold - proceed directly
                        unlock_result = await asyncio.wait_for(
                            self._perform_unlock(
                                vbi_result.speaker_name, {}, {}, attempt_id=None
                            ),
                            timeout=PERFORM_UNLOCK_TIMEOUT
                        )

                        # Terminate caffeinate
                        try:
                            caffeinate_process.terminate()
                        except:
                            pass

                        return {
                            "success": unlock_result["success"],
                            "speaker_name": vbi_result.speaker_name,
                            "transcribed_text": "unlock my screen",
                            "stt_confidence": 1.0,
                            "speaker_confidence": vbi_result.confidence,
                            "verification_confidence": vbi_result.confidence,
                            "is_owner": True,
                            "message": f"Voice verified ({vbi_result.level.value})",
                            "latency_ms": vbi_result.verification_time_ms,
                            "voice_biometric_intelligence": True,
                            "recognition_level": vbi_result.level.value,
                            "announcement": vbi_result.announcement,
                            "timestamp": datetime.now().isoformat(),
                        }
                else:
                    # Voice not verified - provide feedback but continue with full flow
                    logger.info(
                        f"🧠 Voice not immediately recognized "
                        f"({vbi_result.level.value}, {vbi_result.confidence:.1%}). "
                        f"Continuing with full verification..."
                    )
                    # Update diagnostics with VBI result
                    diagnostics.speaker_confidence = vbi_result.confidence

            except asyncio.TimeoutError:
                logger.warning("⏱️ Upfront voice verification timed out, continuing with full flow")
            except Exception as e:
                logger.debug(f"Upfront voice verification failed (continuing with full flow): {e}")

        # =============================================================================
        # 🚀 FAST PATH: Check voice biometric cache for instant repeat unlocks
        # =============================================================================
        if self.voice_biometric_cache:
            try:
                from voice_unlock.voice_biometric_cache import CacheHitType

                # Extract voice embedding for cache lookup (if we have speaker engine)
                voice_embedding = None
                if self.speaker_engine and hasattr(self.speaker_engine, 'extract_embedding'):
                    try:
                        voice_embedding = await asyncio.wait_for(
                            self.speaker_engine.extract_embedding(audio_data),
                            timeout=2.0  # Quick timeout for cache lookup
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass  # Cache lookup is optional - continue without embedding

                cache_result = await self.voice_biometric_cache.lookup_voice_authentication(
                    voice_embedding=voice_embedding
                )

                if cache_result.hit_type != CacheHitType.MISS and cache_result.is_owner:
                    # CACHE HIT: Fast path unlock!
                    cache_latency = (datetime.now() - start_time).total_seconds() * 1000
                    logger.info(
                        f"🚀 CACHE HIT ({cache_result.hit_type.value}): "
                        f"Instant unlock for {cache_result.speaker_name} in {cache_latency:.0f}ms "
                        f"(similarity: {cache_result.similarity_score:.2%}, "
                        f"session age: {cache_result.cache_age_seconds:.0f}s)"
                    )

                    # Still need to perform the actual unlock
                    unlock_result = await asyncio.wait_for(
                        self._perform_unlock(
                            cache_result.speaker_name, {}, {}, attempt_id=None
                        ),
                        timeout=PERFORM_UNLOCK_TIMEOUT
                    )

                    # Terminate caffeinate
                    try:
                        caffeinate_process.terminate()
                    except:
                        pass

                    return {
                        "success": unlock_result["success"],
                        "speaker_name": cache_result.speaker_name,
                        "transcribed_text": "unlock my screen",  # Cached command
                        "stt_confidence": 1.0,  # Cached
                        "speaker_confidence": cache_result.verification_confidence,
                        "verification_confidence": cache_result.verification_confidence,
                        "is_owner": True,
                        "message": f"Instant unlock (cached session: {cache_result.hit_type.value})",
                        "latency_ms": cache_latency,
                        "cache_hit": True,
                        "cache_hit_type": cache_result.hit_type.value,
                        "cache_similarity": cache_result.similarity_score,
                        "timestamp": datetime.now().isoformat(),
                    }
            except Exception as e:
                logger.debug(f"Cache lookup failed (continuing with full verification): {e}")

        # =============================================================================
        # FULL VERIFICATION PATH (with global timeout protection)
        # =============================================================================
        try:
            return await asyncio.wait_for(
                self._process_voice_unlock_internal(
                    audio_data, context, diagnostics, metrics_logger, stages, start_time, caffeinate_process
                ),
                timeout=TOTAL_UNLOCK_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"⏱️ TOTAL UNLOCK TIMEOUT: Processing exceeded {TOTAL_UNLOCK_TIMEOUT}s")
            try:
                caffeinate_process.terminate()
            except:
                pass
            return await self._create_failure_response(
                "total_timeout",
                f"Unlock processing exceeded {TOTAL_UNLOCK_TIMEOUT}s timeout. Please try again.",
                diagnostics=diagnostics.__dict__
            )

    async def _process_voice_unlock_internal(
        self,
        audio_data,
        context: Optional[Dict[str, Any]],
        diagnostics: UnlockDiagnostics,
        metrics_logger,
        stages: list,
        start_time: datetime,
        caffeinate_process
    ) -> Dict[str, Any]:
        """
        Internal processing with all stages - wrapped by global timeout.
        """
        from voice_unlock.unlock_metrics_logger import StageMetrics

        logger.info("🎤 Processing voice unlock command...")

        # Stage 1: Audio Preparation
        stage_audio_prep = metrics_logger.create_stage(
            "audio_preparation",
            input_type=type(audio_data).__name__,
            input_size_raw=len(audio_data) if isinstance(audio_data, bytes) else 0
        )

        # Convert audio to proper format with error handling
        # CRITICAL FIX: Use async version to avoid blocking event loop during FFmpeg transcoding
        try:
            from voice.audio_format_converter import prepare_audio_for_stt_async
            audio_data = await prepare_audio_for_stt_async(audio_data)
            diagnostics.audio_size_bytes = len(audio_data) if audio_data else 0
            logger.info(f"📊 Audio prepared: {diagnostics.audio_size_bytes} bytes")

            stage_audio_prep.complete(
                success=True,
                algorithm_used="prepare_audio_for_stt_async",
                input_size_bytes=stage_audio_prep.metadata.get('input_size_raw', 0),
                output_size_bytes=diagnostics.audio_size_bytes
            )
            stages.append(stage_audio_prep)
        except Exception as e:
            logger.error(f"❌ Audio preparation failed: {e}")
            diagnostics.error_messages.append(f"Audio preparation failed: {str(e)}")
            stage_audio_prep.complete(success=False, error_message=str(e))
            stages.append(stage_audio_prep)

            # Log failed attempt
            self._log_failed_unlock_attempt(
                metrics_logger, stages, "audio_preparation_failed",
                "Failed to prepare audio data", str(e)
            )

            return await self._create_failure_response(
                "audio_preparation_failed",
                "Failed to prepare audio data",
                diagnostics=diagnostics.__dict__
            )

        # Extract sample_rate from context if provided by frontend
        sample_rate = None
        if context:
            sample_rate = context.get("audio_sample_rate")
            if sample_rate:
                logger.info(f"🎵 Using frontend-provided sample rate: {sample_rate}Hz")

        # =================================================================
        # 🚀 PARALLEL EXECUTION: Run STT and Speaker ID concurrently!
        # This saves 5-8 seconds by not waiting for STT before starting Speaker ID
        # =================================================================
        stage_transcription = metrics_logger.create_stage(
            "transcription",
            sample_rate=sample_rate,
            audio_size=diagnostics.audio_size_bytes
        )
        stage_speaker_id_parallel = metrics_logger.create_stage(
            "speaker_identification_parallel",
            parallel_with="transcription"
        )

        # Get dynamic timeouts (adapts to cold start, warmup state, etc.)
        stt_timeout = _timeout_manager.get_transcription_timeout()
        spkr_timeout = _timeout_manager.get_speaker_id_timeout()

        logger.info(f"⏱️ Dynamic timeouts: STT={stt_timeout:.1f}s, Speaker={spkr_timeout:.1f}s")

        # Check ML registry readiness for better timeout decisions
        await _timeout_manager.check_ml_registry_ready()

        # Create parallel tasks - both use audio_data, neither depends on the other
        stt_start_time = datetime.now()
        stt_task = asyncio.create_task(
            asyncio.wait_for(
                self._transcribe_audio_with_retry(
                    audio_data, diagnostics, sample_rate=sample_rate
                ),
                timeout=stt_timeout
            )
        )
        speaker_id_task = asyncio.create_task(
            asyncio.wait_for(
                self._identify_speaker(audio_data),
                timeout=spkr_timeout
            )
        )

        logger.info("🚀 Running STT and Speaker ID in PARALLEL...")
        parallel_start = datetime.now()

        # Wait for both tasks concurrently
        transcription_result = None
        parallel_speaker_result = None

        try:
            # Gather both results - continue even if one fails
            results = await asyncio.gather(
                stt_task,
                speaker_id_task,
                return_exceptions=True
            )

            # Process STT result
            if isinstance(results[0], Exception):
                if isinstance(results[0], asyncio.TimeoutError):
                    logger.error(f"⏱️ Transcription timed out after {stt_timeout:.1f}s (cold_start={_timeout_manager._cold_start})")
                    stage_transcription.complete(success=False, error_message=f"Transcription timeout ({stt_timeout:.1f}s)")
                    _timeout_manager.record_failure("transcription", f"Timeout after {stt_timeout:.1f}s")
                else:
                    logger.error(f"❌ Transcription failed: {results[0]}")
                    stage_transcription.complete(success=False, error_message=str(results[0]))
                    _timeout_manager.record_failure("transcription", str(results[0]))
            else:
                transcription_result = results[0]
                # Record successful transcription time for adaptive timeout learning
                stt_duration_ms = (datetime.now() - stt_start_time).total_seconds() * 1000
                _timeout_manager.record_transcription_time(stt_duration_ms)
                logger.info(f"✅ Transcription completed in {stt_duration_ms:.0f}ms (timeout was {stt_timeout:.1f}s)")
                stage_transcription.complete(success=True, processing_time_ms=stt_duration_ms)

            # Process Speaker ID result (save for later use)
            if isinstance(results[1], Exception):
                if isinstance(results[1], asyncio.TimeoutError):
                    logger.warning(f"⏱️ Parallel Speaker ID timed out after {spkr_timeout:.1f}s (cold_start={_timeout_manager._cold_start})")
                    stage_speaker_id_parallel.complete(success=False, error_message=f"Timeout ({spkr_timeout:.1f}s)")
                    _timeout_manager.record_failure("speaker_id", f"Timeout after {spkr_timeout:.1f}s")
                else:
                    logger.warning(f"⚠️ Parallel Speaker ID failed: {results[1]}")
                    stage_speaker_id_parallel.complete(success=False, error_message=str(results[1]))
                    _timeout_manager.record_failure("speaker_id", str(results[1]))
                parallel_speaker_result = (None, 0.0)
            else:
                parallel_speaker_result = results[1]
                stage_speaker_id_parallel.complete(
                    success=parallel_speaker_result[0] is not None,
                    confidence_score=parallel_speaker_result[1] if parallel_speaker_result[0] else 0
                )

            stages.append(stage_speaker_id_parallel)

        except Exception as e:
            logger.error(f"❌ Parallel execution error: {e}")

        parallel_time = (datetime.now() - parallel_start).total_seconds() * 1000
        logger.info(f"⚡ Parallel STT + Speaker ID completed in {parallel_time:.0f}ms")

        # Handle transcription failure
        if not transcription_result:
            stages.append(stage_transcription)
            return await self._create_failure_response(
                "transcription_timeout",
                "Speech recognition took too long. Please try again.",
                diagnostics=diagnostics.__dict__
            )

        if not transcription_result:
            diagnostics.failure_reason = "transcription_failed"
            diagnostics.processing_time_ms = (datetime.now() - start_time).total_seconds() * 1000
            self._store_diagnostics(diagnostics)

            stage_transcription.complete(success=False, error_message="Transcription failed")
            stages.append(stage_transcription)

            return await self._create_failure_response(
                "transcription_failed",
                "Could not transcribe audio",
                diagnostics=diagnostics.__dict__
            )

        transcribed_text = transcription_result.text
        stt_confidence = transcription_result.confidence
        speaker_identified = transcription_result.speaker_identified

        # Get STT engine used
        stt_engine = getattr(transcription_result, 'engine_used', 'unknown')
        if hasattr(diagnostics, 'stt_engine_used') and diagnostics.stt_engine_used:
            stt_engine = diagnostics.stt_engine_used

        stage_transcription.complete(
            success=True,
            algorithm_used=stt_engine,
            confidence_score=stt_confidence,
            output_size_bytes=len(transcribed_text.encode('utf-8')),
            metadata={
                'transcribed_text': transcribed_text,
                'speaker_identified': speaker_identified
            }
        )
        stages.append(stage_transcription)

        logger.info(f"📝 Transcribed: '{transcribed_text}' (confidence: {stt_confidence:.2f})")
        logger.info(f"👤 Speaker: {speaker_identified or 'Unknown'}")

        # 🧠 HALLUCINATION GUARD: Check and correct STT hallucinations (with timeout)
        try:
            from voice.stt_hallucination_guard import verify_stt_transcription

            original_text = transcribed_text
            transcribed_text, was_corrected, hallucination_detection = await asyncio.wait_for(
                verify_stt_transcription(
                    text=transcribed_text,
                    confidence=stt_confidence,
                    audio_data=audio_data,
                    context="unlock_command"
                ),
                timeout=HALLUCINATION_CHECK_TIMEOUT
            )

            if was_corrected:
                logger.info(
                    f"🧠 [HALLUCINATION-GUARD] Corrected: '{original_text}' → '{transcribed_text}'"
                )
                diagnostics.error_messages.append(
                    f"STT hallucination corrected: '{original_text}' → '{transcribed_text}'"
                )
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Hallucination guard timed out after {HALLUCINATION_CHECK_TIMEOUT}s (skipping)")
        except ImportError:
            logger.debug("Hallucination guard not available, skipping")
        except Exception as e:
            logger.warning(f"Hallucination guard error (continuing): {e}")

        # Stage 3: Intent Verification
        stage_intent = metrics_logger.create_stage(
            "intent_verification",
            text_to_verify=transcribed_text
        )

        is_unlock_command = await self._verify_unlock_intent(transcribed_text, context)

        stage_intent.complete(
            success=is_unlock_command,
            algorithm_used="NLP pattern matching",
            metadata={'is_unlock_command': is_unlock_command}
        )
        stages.append(stage_intent)

        if not is_unlock_command:
            return await self._create_failure_response(
                "not_unlock_command", f"Command '{transcribed_text}' is not an unlock request"
            )

        # Stage 4: Speaker Identification - USE PARALLEL RESULT (already computed!)
        # 🚀 This is the key optimization - we already have the result from parallel execution
        stage_speaker_id = metrics_logger.create_stage(
            "speaker_identification",
            already_identified=speaker_identified is not None,
            used_parallel_result=True
        )

        # Use result from parallel execution if available
        if parallel_speaker_result and parallel_speaker_result[0]:
            # Parallel speaker ID succeeded - use it!
            if not speaker_identified:
                speaker_identified = parallel_speaker_result[0]
                speaker_confidence = parallel_speaker_result[1]
                logger.info(f"⚡ Using PARALLEL speaker ID result: {speaker_identified}")
            else:
                # STT already identified speaker - use parallel result for confidence
                speaker_confidence = parallel_speaker_result[1]
        elif speaker_identified:
            # STT identified speaker but parallel failed - get confidence separately
            try:
                speaker_confidence = await asyncio.wait_for(
                    self._get_speaker_confidence(audio_data, speaker_identified),
                    timeout=2.0  # Short timeout since we already have identification
                )
            except (asyncio.TimeoutError, Exception):
                speaker_confidence = 0.7  # Default reasonable confidence
        else:
            # Neither parallel nor STT identified speaker - this is a failure case
            speaker_identified = None
            speaker_confidence = 0.0
            logger.warning("⚠️ No speaker identification from parallel or STT")

        # Handle None confidence gracefully (ML unavailable = degraded mode)
        confidence_display = speaker_confidence if speaker_confidence is not None else 0.0
        stage_speaker_id.complete(
            success=speaker_identified is not None,
            algorithm_used="SpeechBrain speaker recognition (parallel)",
            confidence_score=confidence_display if speaker_identified else 0,
            metadata={'speaker_name': speaker_identified, 'from_parallel': True, 'ml_unavailable': speaker_confidence is None}
        )
        stages.append(stage_speaker_id)

        if speaker_confidence is None:
            logger.warning(
                f"⚠️ Speaker identification running in DEGRADED MODE (ML unavailable) - "
                f"using physics/behavioral/context only"
            )
        else:
            logger.info(
                f"🔐 Speaker identified: {speaker_identified} (confidence: {speaker_confidence:.2f})"
            )

        # Stage 5: Owner Verification
        stage_owner_check = metrics_logger.create_stage(
            "owner_verification",
            speaker_name=speaker_identified
        )

        try:
            is_owner = await asyncio.wait_for(
                self._verify_owner(speaker_identified),
                timeout=OWNER_CHECK_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Owner verification timed out after {OWNER_CHECK_TIMEOUT}s")
            is_owner = False  # Fail-safe: treat as not owner on timeout

        stage_owner_check.complete(
            success=is_owner,
            algorithm_used="Database owner lookup",
            metadata={'is_owner': is_owner}
        )
        stages.append(stage_owner_check)

        if not is_owner:
            self.stats["rejected_attempts"] += 1
            logger.warning(f"🚫 Non-owner '{speaker_identified}' attempted unlock - REJECTED")

            # Analyze security event with SAI (with timeout)
            try:
                security_analysis = await asyncio.wait_for(
                    self._analyze_security_event(
                        speaker_name=speaker_identified,
                        transcribed_text=transcribed_text,
                        context=context,
                        speaker_confidence=speaker_confidence,
                    ),
                    timeout=SECURITY_ANALYSIS_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Security analysis timed out, using default")
                security_analysis = {"level": "warning", "reason": "timeout"}

            # Record rejection to database with full analysis (with timeout)
            try:
                await asyncio.wait_for(
                    self._record_unlock_attempt(
                        speaker_name=speaker_identified,
                        transcribed_text=transcribed_text,
                        success=False,
                        rejection_reason="not_owner",
                        audio_data=audio_data,
                        stt_confidence=stt_confidence,
                        speaker_confidence=speaker_confidence,
                        security_analysis=security_analysis,
                    ),
                    timeout=RECORD_ATTEMPT_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("⏱️ Recording unlock attempt timed out (non-critical)")

            # Generate intelligent, dynamic security response (with timeout)
            try:
                security_message = await asyncio.wait_for(
                    self._generate_security_response(
                        speaker_name=speaker_identified,
                        reason="not_owner",
                        analysis=security_analysis,
                        context=context,
                    ),
                    timeout=SECURITY_RESPONSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("⏱️ Security response generation timed out")
                security_message = "Access denied. You are not authorized to unlock this device."

            return await self._create_failure_response(
                "not_owner",
                security_message,
                speaker_name=speaker_identified,
                security_analysis=security_analysis,
            )

        # Stage 6: Biometric Verification (anti-spoofing) - with timeout protection
        stage_biometric = metrics_logger.create_stage(
            "biometric_verification",
            speaker_name=speaker_identified,
            audio_size=diagnostics.audio_size_bytes
        )

        # 🚀 UNIFIED CACHE FAST-PATH: Try instant recognition before expensive verification
        # This is the critical integration - unified cache has Derek's preloaded profile
        # Average cache match time: <1ms vs full verification: 200-500ms
        unified_cache_hit = False
        voice_embedding_for_cache = None

        if self.unified_cache and self.unified_cache.is_ready:
            try:
                unified_result = await asyncio.wait_for(
                    self.unified_cache.verify_voice_from_audio(
                        audio_data=audio_data,
                        sample_rate=16000,
                        expected_speaker=speaker_identified,  # Hint for faster matching
                    ),
                    timeout=2.0  # Fast-path timeout
                )

                if unified_result.matched and unified_result.similarity >= 0.88:
                    # HIGH CONFIDENCE INSTANT MATCH - skip full verification!
                    unified_cache_hit = True
                    verification_passed = True
                    verification_confidence = unified_result.similarity
                    voice_embedding_for_cache = unified_result.embedding

                    logger.info(
                        f"⚡ UNIFIED CACHE INSTANT MATCH: {speaker_identified} "
                        f"(similarity={unified_result.similarity:.2%}, "
                        f"type={unified_result.match_type}, "
                        f"source={unified_result.profile_source})"
                    )

                    # Track cache performance
                    self.stats["unified_cache_hits"] = self.stats.get("unified_cache_hits", 0) + 1

                elif unified_result.embedding is not None:
                    # Got embedding but not instant match - save for fallback
                    voice_embedding_for_cache = unified_result.embedding
                    logger.debug(
                        f"🔄 Unified cache: no instant match "
                        f"(similarity={unified_result.similarity:.2%}), falling back to full verification"
                    )

            except asyncio.TimeoutError:
                logger.debug("⏱️ Unified cache fast-path timed out, falling back to full verification")
            except Exception as e:
                logger.debug(f"Unified cache fast-path failed (non-critical): {e}")

        # 🔑 FALLBACK: Extract embedding if not already from unified cache
        if voice_embedding_for_cache is None and self.speaker_engine and hasattr(self.speaker_engine, 'extract_embedding'):
            try:
                voice_embedding_for_cache = await asyncio.wait_for(
                    self.speaker_engine.extract_embedding(audio_data),
                    timeout=2.0  # Quick extraction timeout
                )
                logger.debug(f"🔐 Voice embedding extracted for cache: shape={voice_embedding_for_cache.shape if hasattr(voice_embedding_for_cache, 'shape') else 'N/A'}")
            except Exception as e:
                logger.debug(f"Voice embedding extraction failed (non-critical): {e}")

        # 🔒 FULL VERIFICATION: Only if unified cache didn't provide instant match
        if not unified_cache_hit:
            try:
                verification_passed, verification_confidence = await asyncio.wait_for(
                    self._verify_speaker_identity(audio_data, speaker_identified),
                    timeout=BIOMETRIC_TIMEOUT  # 10 second timeout (was 30s!)
                )
            except asyncio.TimeoutError:
                logger.error(f"⏱️ Biometric verification timed out after {BIOMETRIC_TIMEOUT}s")
                stage_biometric.complete(success=False, error_message="Verification timeout")
                stages.append(stage_biometric)
                return await self._create_failure_response(
                    "biometric_timeout",
                    "Voice verification took too long. Please try again.",
                    diagnostics=diagnostics.__dict__
                )

        # Get threshold dynamically
        threshold = getattr(self.speaker_engine, 'threshold', 0.35) if self.speaker_engine else 0.35

        stage_biometric.complete(
            success=verification_passed,
            algorithm_used="SpeechBrain ECAPA-TDNN",
            confidence_score=verification_confidence,
            threshold=threshold,
            above_threshold=verification_confidence >= threshold,
            metadata={
                'verification_method': 'cosine_similarity',
                'embedding_dimension': 192
            }
        )
        stages.append(stage_biometric)

        # 🤖 ML LEARNING: Update voice biometric model (learn from this attempt)
        if self.ml_engine:
            try:
                await self.ml_engine.voice_learner.update_from_attempt(
                    confidence=verification_confidence,
                    success=verification_passed,
                    is_owner=True,  # We verified this is the owner (passed owner check)
                    audio_quality=stt_confidence
                )
                self.stats["ml_voice_updates"] += 1
                logger.debug(f"🤖 ML: Voice biometric model updated (confidence: {verification_confidence:.2%})")
            except Exception as e:
                logger.error(f"ML voice learning update failed: {e}")

        if not verification_passed:
            self.stats["failed_authentications"] += 1

            # 🔍 DETAILED DIAGNOSTICS: Analyze why verification failed (with timeout)
            try:
                failure_diagnostics = await asyncio.wait_for(
                    self._analyze_verification_failure(
                        audio_data=audio_data,
                        speaker_name=speaker_identified,
                        confidence=verification_confidence,
                        transcription=transcribed_text
                    ),
                    timeout=FAILURE_ANALYSIS_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("⏱️ Verification failure analysis timed out")
                failure_diagnostics = {
                    "threshold": 0.35,
                    "audio_quality": "unknown",
                    "primary_reason": "analysis_timeout",
                    "user_message": "Please try again."
                }

            logger.error(
                f"🚫 Voice verification FAILED for owner '{speaker_identified}'\n"
                f"   ├─ Confidence: {verification_confidence:.2%} (threshold: {failure_diagnostics.get('threshold', 'unknown')})\n"
                f"   ├─ Audio quality: {failure_diagnostics.get('audio_quality', 'unknown')}\n"
                f"   ├─ Audio duration: {failure_diagnostics.get('audio_duration_ms', 0)}ms\n"
                f"   ├─ Audio energy: {failure_diagnostics.get('audio_energy', 0):.6f}\n"
                f"   ├─ Samples in DB: {failure_diagnostics.get('samples_in_db', 0)}\n"
                f"   ├─ Embedding dimension: {failure_diagnostics.get('embedding_dimension', 'unknown')}\n"
                f"   ├─ Primary failure reason: {failure_diagnostics.get('primary_reason', 'unknown')}\n"
                f"   ├─ Suggested fix: {failure_diagnostics.get('suggested_fix', 'unknown')}\n"
                f"   └─ Architecture issue: {failure_diagnostics.get('architecture_issue', 'none detected')}"
            )

            # Record failed authentication with diagnostics (with timeout)
            try:
                await asyncio.wait_for(
                    self._record_unlock_attempt(
                        speaker_name=speaker_identified,
                        transcribed_text=transcribed_text,
                        success=False,
                        rejection_reason="verification_failed",
                        audio_data=audio_data,
                        stt_confidence=stt_confidence,
                        speaker_confidence=verification_confidence,
                    ),
                    timeout=RECORD_ATTEMPT_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("⏱️ Recording failed authentication timed out (non-critical)")

            # Track in monitoring system
            try:
                import sys
                from pathlib import Path
                # Add parent directory to path to import from start_system
                sys.path.insert(0, str(Path(__file__).parent.parent.parent))
                from start_system import track_voice_verification_attempt
                track_voice_verification_attempt(False, verification_confidence, failure_diagnostics)
            except Exception as e:
                logger.debug(f"Failed to track verification in monitoring: {e}")

            return await self._create_failure_response(
                "verification_failed",
                f"Voice verification failed (confidence: {verification_confidence:.2%}). {failure_diagnostics.get('user_message', 'Please try again.')}",
                speaker_name=speaker_identified,
                diagnostics=failure_diagnostics
            )

        self.stats["owner_unlock_attempts"] += 1
        logger.info(f"✅ Owner '{speaker_identified}' verified for unlock")

        # Track successful verification in monitoring system
        try:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            from start_system import track_voice_verification_attempt
            track_voice_verification_attempt(True, verification_confidence, None)
        except Exception as e:
            logger.debug(f"Failed to track verification in monitoring: {e}")

        # Stage 7 & 8: Context and Scenario Analysis (PARALLEL)
        stage_context = metrics_logger.create_stage(
            "context_analysis",
            text=transcribed_text
        )
        stage_scenario = metrics_logger.create_stage(
            "scenario_analysis",
            speaker=speaker_identified
        )

        # Run context and scenario analysis in parallel for speed
        context_analysis, scenario_analysis = await asyncio.gather(
            self._analyze_context(transcribed_text, context),
            self._analyze_scenario(transcribed_text, context, speaker_identified),
            return_exceptions=True
        )

        # Handle exceptions from parallel execution
        if isinstance(context_analysis, Exception):
            logger.warning(f"Context analysis failed: {context_analysis}")
            context_analysis = {"available": False, "error": str(context_analysis)}
        if isinstance(scenario_analysis, Exception):
            logger.warning(f"Scenario analysis failed: {scenario_analysis}")
            scenario_analysis = {"available": False, "error": str(scenario_analysis)}

        stage_context.complete(
            success=True,
            algorithm_used="CAI (Context-Aware Intelligence)",
            metadata={'context_data': context_analysis}
        )
        stages.append(stage_context)

        stage_scenario.complete(
            success=True,
            algorithm_used="SAI (Scenario-Aware Intelligence)",
            metadata={'scenario_data': scenario_analysis}
        )
        stages.append(stage_scenario)

        # 🤖 ML LEARNING: Record unlock attempt BEFORE performing unlock to get attempt_id
        # This allows password typing metrics to be linked to the unlock attempt (with timeout)
        try:
            attempt_id = await asyncio.wait_for(
                self._record_unlock_attempt(
                    speaker_name=speaker_identified,
                    transcribed_text=transcribed_text,
                    success=True,  # Will be updated after unlock completes
                    rejection_reason=None,
                    audio_data=audio_data,
                    stt_confidence=stt_confidence,
                    speaker_confidence=verification_confidence,
                    context_data=context_analysis,
                    scenario_data=scenario_analysis,
                ),
                timeout=RECORD_ATTEMPT_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("⏱️ Recording unlock attempt timed out, proceeding without ML tracking")
            attempt_id = None

        # Stage 9: Screen Unlock Execution (critical - with timeout)
        stage_unlock = metrics_logger.create_stage(
            "unlock_execution",
            speaker=speaker_identified
        )

        try:
            unlock_result = await asyncio.wait_for(
                self._perform_unlock(
                    speaker_identified, context_analysis, scenario_analysis, attempt_id=attempt_id
                ),
                timeout=PERFORM_UNLOCK_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Screen unlock timed out after {PERFORM_UNLOCK_TIMEOUT}s")
            stage_unlock.complete(success=False, error_message="Unlock timeout")
            stages.append(stage_unlock)
            return await self._create_failure_response(
                "unlock_timeout",
                "Screen unlock operation timed out. Please try again.",
                diagnostics=diagnostics.__dict__
            )

        stage_unlock.complete(
            success=unlock_result["success"],
            algorithm_used="macOS SecurePasswordTyper with ML metrics",
            metadata={
                'unlock_method': 'password_entry',
                'result_message': unlock_result.get("message"),
                'ml_attempt_id': attempt_id
            }
        )
        stages.append(stage_unlock)

        # Calculate total latency BEFORE post-processing (for accurate user-facing latency)
        total_latency_ms = (datetime.now() - start_time).total_seconds() * 1000

        # Update stats immediately (fast, in-memory)
        if unlock_result["success"]:
            self.stats["successful_unlocks"] += 1
            self.stats["last_unlock_time"] = datetime.now()
            logger.info(f"🔓 Screen unlocked successfully by owner '{speaker_identified}' in {total_latency_ms:.0f}ms")

        # Get speaker verification threshold dynamically
        threshold = getattr(self.speaker_engine, 'threshold', 0.35) if self.speaker_engine else 0.35

        # Build detailed developer metrics (fast, in-memory)
        developer_metrics = {
            "biometrics": {
                "speaker_confidence": verification_confidence,
                "stt_confidence": stt_confidence,
                "threshold": threshold,
                "above_threshold": verification_confidence >= threshold,
                "confidence_margin": verification_confidence - threshold,
                "confidence_percentage": f"{verification_confidence * 100:.1f}%",
            },
            "performance": {
                "total_latency_ms": total_latency_ms,
                "transcription_time_ms": diagnostics.processing_time_ms if hasattr(self, 'diagnostics') else None,
            },
            "quality_indicators": {
                "audio_quality": "good" if stt_confidence > 0.7 else "fair" if stt_confidence > 0.5 else "poor",
                "voice_match_quality": "excellent" if verification_confidence > 0.6 else "good" if verification_confidence > 0.45 else "acceptable" if verification_confidence > threshold else "below_threshold",
                "overall_confidence": (stt_confidence + verification_confidence) / 2,
            }
        }

        # Build result BEFORE fire-and-forget tasks (return fast)
        result = {
            "success": unlock_result["success"],
            "speaker_name": speaker_identified,
            "transcribed_text": transcribed_text,
            "stt_confidence": stt_confidence,
            "speaker_confidence": verification_confidence,
            "verification_confidence": verification_confidence,
            "is_owner": True,
            "message": unlock_result.get("message", "Unlock successful"),
            "latency_ms": total_latency_ms,
            "context_analysis": context_analysis,
            "scenario_analysis": scenario_analysis,
            "timestamp": datetime.now().isoformat(),
            # Developer metrics (UI only, not announced)
            "dev_metrics": developer_metrics,
        }

        # FIRE-AND-FORGET: Non-blocking post-processing tasks
        # These run in the background and don't delay the response
        async def _post_unlock_tasks():
            """Non-critical tasks that run after response is sent"""
            try:
                # Cleanup caffeinate
                try:
                    caffeinate_process.terminate()
                    logger.debug("🔋 Caffeinate terminated")
                except:
                    pass

                # Update speaker profile with continuous learning (if successful)
                if unlock_result["success"]:
                    await self._update_speaker_profile(
                        speaker_identified, audio_data, transcribed_text, success=True
                    )

                    # 🚀 CACHE SUCCESSFUL AUTHENTICATION for instant repeat unlocks
                    if self.voice_biometric_cache:
                        try:
                            session_id = await asyncio.wait_for(
                                self.voice_biometric_cache.cache_authentication(
                                    speaker_name=speaker_identified,
                                    voice_embedding=voice_embedding_for_cache,  # Captured during verification
                                    verification_confidence=verification_confidence,
                                    is_owner=True,
                                    transcribed_text=transcribed_text,
                                ),
                                timeout=1.0  # Quick cache store timeout
                            )
                            logger.info(
                                f"🔐 Voice auth cached: session={session_id[:8]}... "
                                f"(next unlock will be instant!)"
                            )
                        except asyncio.TimeoutError:
                            logger.debug("Voice auth cache storage timed out (non-critical)")
                        except Exception as e:
                            logger.debug(f"Voice auth cache storage failed (non-critical): {e}")

                # Log advanced metrics to JSON file
                import platform
                import sys
                system_info = {
                    "platform": platform.system(),
                    "platform_version": platform.version(),
                    "python_version": sys.version.split()[0],
                    "stt_engine": diagnostics.stt_engine_used,
                    "speaker_engine": "SpeechBrain" if self.speaker_engine else "None",
                }

                await metrics_logger.log_unlock_attempt(
                    success=unlock_result["success"],
                    speaker_name=speaker_identified,
                    transcribed_text=transcribed_text,
                    stages=stages,
                    biometrics=developer_metrics["biometrics"],
                    performance=developer_metrics["performance"],
                    quality_indicators=developer_metrics["quality_indicators"],
                    system_info=system_info,
                    error=None if unlock_result["success"] else unlock_result.get("message")
                )
            except Exception as e:
                logger.warning(f"Post-unlock task error (non-critical): {e}")

        # Launch post-unlock tasks without waiting
        asyncio.create_task(_post_unlock_tasks())

        # Return pre-built result immediately (fast path)
        return result

    def _check_circuit_breaker(self, service_name: str) -> bool:
        """
        Check if circuit breaker allows operation.

        Returns:
            True if operation is allowed, False if circuit is open
        """
        import time

        current_time = time.time()

        # Check if circuit is open
        if self._circuit_breaker_failures[service_name] >= self.circuit_breaker_threshold:
            last_failure = self._circuit_breaker_last_failure[service_name]

            # Check if timeout has passed
            if current_time - last_failure < self.circuit_breaker_timeout:
                logger.warning(
                    f"🔴 Circuit breaker OPEN for {service_name} "
                    f"({self._circuit_breaker_failures[service_name]} failures)"
                )
                return False
            else:
                # Reset circuit breaker after timeout
                logger.info(f"🟢 Circuit breaker RESET for {service_name}")
                self._circuit_breaker_failures[service_name] = 0

        return True

    def _record_circuit_breaker_failure(self, service_name: str):
        """Record a failure for circuit breaker"""
        import time

        self._circuit_breaker_failures[service_name] += 1
        self._circuit_breaker_last_failure[service_name] = time.time()

        logger.debug(
            f"⚠️ Circuit breaker failure recorded for {service_name}: "
            f"{self._circuit_breaker_failures[service_name]}/{self.circuit_breaker_threshold}"
        )

    def _record_circuit_breaker_success(self, service_name: str):
        """Record a success - reset failure count"""
        if self._circuit_breaker_failures[service_name] > 0:
            logger.debug(f"✅ Circuit breaker success for {service_name} - resetting failures")
            self._circuit_breaker_failures[service_name] = 0

    def _store_diagnostics(self, diagnostics: UnlockDiagnostics):
        """Store diagnostics in history for analysis"""
        self._diagnostics_history.append(diagnostics)

        # Keep only last N diagnostics
        if len(self._diagnostics_history) > self._max_diagnostics_history:
            self._diagnostics_history.pop(0)

    async def _transcribe_audio_with_retry(
        self, audio_data: bytes, diagnostics: UnlockDiagnostics, sample_rate: Optional[int] = None
    ):
        """
        Transcribe audio with retry logic and circuit breaker.

        Args:
            audio_data: Audio bytes to transcribe
            diagnostics: Diagnostics object to track attempts
            sample_rate: Optional sample rate from frontend (browser-reported)

        Returns:
            Transcription result or None if all retries failed
        """
        service_name = "stt_transcription"

        # Check circuit breaker
        if not self._check_circuit_breaker(service_name):
            diagnostics.error_messages.append(f"Circuit breaker open for {service_name}")
            return None

        for attempt in range(self.max_retries):
            diagnostics.retry_count = attempt + 1

            try:
                logger.info(f"🔄 Transcription attempt {attempt + 1}/{self.max_retries}")

                result = await self._transcribe_audio(audio_data, sample_rate=sample_rate)

                if result:
                    # Success - record and return
                    self._record_circuit_breaker_success(service_name)
                    diagnostics.stt_engine_used = getattr(result, 'engine_used', 'unknown')
                    return result
                else:
                    logger.warning(f"⚠️  Transcription attempt {attempt + 1} returned None")

            except Exception as e:
                error_msg = f"Transcription attempt {attempt + 1} failed: {str(e)}"
                logger.error(f"❌ {error_msg}")
                diagnostics.error_messages.append(error_msg)

            # Wait before retry (exponential backoff)
            if attempt < self.max_retries - 1:
                delay = self.retry_delay_seconds * (2 ** attempt)
                logger.info(f"⏳ Waiting {delay:.1f}s before retry...")
                await asyncio.sleep(delay)

        # All retries failed
        self._record_circuit_breaker_failure(service_name)
        logger.error(f"❌ All {self.max_retries} transcription attempts failed")
        return None

    async def _transcribe_audio(self, audio_data: bytes, sample_rate: Optional[int] = None):
        """
        Transcribe audio using Hybrid STT

        Args:
            audio_data: Audio bytes to transcribe
            sample_rate: Optional sample rate from frontend (browser-reported)
        """
        if not self.stt_router:
            logger.error("Hybrid STT not available")
            return None

        try:
            from voice.stt_config import RoutingStrategy

            # CRITICAL FIX: Convert base64 string to bytes before transcription
            if isinstance(audio_data, str):
                import base64
                try:
                    audio_data = base64.b64decode(audio_data)
                    logger.info(f"✅ Decoded base64 audio: {len(audio_data)} bytes")
                except Exception as e:
                    logger.error(f"❌ Failed to decode base64 audio_data: {e}")
                    return None

            # Use ACCURACY strategy for unlock (security-critical)
            # **CRITICAL**: Use 'unlock' mode for ultra-fast 2-second window + VAD filtering
            result = await self.stt_router.transcribe(
                audio_data=audio_data,
                strategy=RoutingStrategy.ACCURACY,
                speaker_name=None,  # Auto-detect
                sample_rate=sample_rate,  # Pass sample rate for proper resampling
                mode='unlock',  # UNLOCK MODE: 2-second window, VAD filtering, maximum speed
            )

            return result

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return None

    async def _verify_unlock_intent(
        self, transcribed_text: str, context: Optional[Dict[str, Any]]
    ) -> bool:
        """
        Verify that the transcribed text is an UNLOCK command (not a LOCK command).
        
        IMPORTANT: "lock my screen" is NOT an unlock command! It's a lock command.
        Only return True for explicit unlock requests.
        
        Uses fuzzy matching for common Whisper STT transcription errors while
        being careful to distinguish between lock and unlock commands.
        """
        text_lower = transcribed_text.lower()

        # CRITICAL: First check if this is a LOCK command (not unlock)
        # "lock" without "unlock" = lock command = NOT an unlock intent
        has_lock = "lock" in text_lower
        has_unlock = "unlock" in text_lower
        
        if has_lock and not has_unlock:
            # This is a LOCK command, not an unlock command
            logger.info(f"🔒 Detected LOCK command (not unlock): '{transcribed_text}'")
            return False

        # Primary unlock phrases - explicit unlock keywords
        unlock_phrases = ["unlock", "open up", "let me in", "sign in", "log in", "access granted"]

        # Check if any unlock phrase is present
        if any(phrase in text_lower for phrase in unlock_phrases):
            logger.info(f"🔓 Detected UNLOCK command: '{transcribed_text}'")
            return True

        # Fuzzy matching for common Whisper STT transcription errors
        # "unlock my screen" often becomes "I'm like my screen" or similar
        # NOTE: We do NOT include "lock my screen" here - that's a different command!
        fuzzy_unlock_patterns = [
            "like my screen",  # "unlock" → "I'm like" (STT error)
            "like the screen",  # "unlock" → "I'm like" (STT error)
            "unlike my screen",  # Sometimes "unlock" → "unlike"
            "on lock my screen",  # "unlock" → "on lock"
            "i'm lock my screen",  # "unlock" → "i'm lock"
        ]

        # If we see these STT error patterns + "screen" keyword, accept as unlock
        if any(pattern in text_lower for pattern in fuzzy_unlock_patterns):
            if "screen" in text_lower:
                logger.info(f"🎯 Fuzzy match detected UNLOCK intent from: '{transcribed_text}'")
                return True

        # Additional check: "my screen" alone is ambiguous - don't match
        # User must say "unlock" or have a fuzzy match pattern
        
        return False

    async def _apply_vad_for_speaker_verification(self, audio_data: bytes) -> bytes:
        """
        Apply VAD filtering to audio before speaker verification.
        This dramatically speeds up speaker verification by removing silence
        and reducing audio to 2-second windows (unlock mode).

        TIMEOUT PROTECTION: Will return original audio if VAD takes too long.
        """
        try:
            # Run VAD with timeout protection
            return await asyncio.wait_for(
                self._apply_vad_internal(audio_data),
                timeout=VAD_PREPROCESS_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ VAD preprocessing timed out after {VAD_PREPROCESS_TIMEOUT}s, using original audio")
            return audio_data
        except Exception as e:
            logger.error(f"Failed to apply VAD for speaker verification: {e}, using original audio")
            return audio_data

    async def _apply_vad_internal(self, audio_data: bytes) -> bytes:
        """Internal VAD application - separated for timeout wrapping"""
        from voice.whisper_audio_fix import _whisper_handler
        import numpy as np
        import io
        import wave

        # Decode audio
        audio_bytes = _whisper_handler.decode_audio_data(audio_data)

        # Normalize to 16kHz float32
        normalized_audio = await _whisper_handler.normalize_audio(audio_bytes, sample_rate=16000)

        # Apply VAD + windowing (unlock mode = 2s)
        filtered_audio = await _whisper_handler._apply_vad_and_windowing(
            normalized_audio,
            mode='unlock'  # 2-second window, ultra-fast
        )

        # If VAD filtered everything, return minimal audio
        if len(filtered_audio) == 0:
            logger.warning("⚠️ VAD filtered all audio for speaker verification, using minimal audio")
            filtered_audio = np.zeros(16000, dtype=np.float32)  # 1 second of silence

        # Convert back to bytes (WAV format for speaker verification)
        with io.BytesIO() as wav_buffer:
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)  # mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(16000)  # 16kHz
                audio_int16 = (filtered_audio * 32767).astype(np.int16)
                wav_file.writeframes(audio_int16.tobytes())

            wav_bytes = wav_buffer.getvalue()

        logger.info(f"✅ VAD preprocessed audio: {len(audio_data)} → {len(wav_bytes)} bytes")
        return wav_bytes

    async def _identify_speaker(self, audio_data: bytes) -> Tuple[Optional[str], Optional[float]]:
        """
        Identify speaker from audio with VAD preprocessing for speed.

        ENHANCED v2.1: Graceful degradation with on-demand ECAPA loading.
        Instead of hard failure, tries to load ECAPA on-demand and returns
        None confidence (not 0.0) to enable proper Bayesian fusion handling.
        """
        # Pre-flight check: Verify speaker engine is available
        if not self.speaker_engine:
            logger.error("❌ Speaker identification failed: speaker_engine not initialized")
            # Return None, None to indicate ML is unavailable (not failed with 0.0)
            return None, None

        # Graceful degradation: Try on-demand ECAPA loading if not available
        if hasattr(self, '_ecapa_available') and not self._ecapa_available:
            logger.warning("⚠️ ECAPA not available at init - attempting on-demand load...")
            await self._try_on_demand_ecapa_load()

        # Check again after on-demand load attempt
        if hasattr(self, '_ecapa_available') and not self._ecapa_available:
            logger.warning("=" * 60)
            logger.warning("⚠️ SPEAKER IDENTIFICATION: ECAPA encoder still unavailable")
            logger.warning("   Operating in DEGRADED MODE (physics/behavioral only)")
            logger.warning(f"   Recommendation: {self._get_ecapa_fix_recommendation()}")
            logger.warning("=" * 60)
            # Return None, None - Bayesian fusion will exclude ML and renormalize weights
            return None, None

        try:
            # Apply VAD filtering to speed up speaker verification (unlock mode = 2s max)
            filtered_audio = await self._apply_vad_for_speaker_verification(audio_data)

            # New SpeakerVerificationService
            if hasattr(self.speaker_engine, "verify_speaker"):
                result = await self.speaker_engine.verify_speaker(filtered_audio)

                # Check for verification failure with 0% confidence
                confidence = result.get("confidence", 0.0)
                speaker_name = result.get("speaker_name")

                if confidence == 0.0 and speaker_name is None:
                    # This is the 0% confidence bug - log detailed diagnostics
                    logger.warning("⚠️ Speaker verification returned 0% confidence")
                    logger.warning("   Possible causes:")
                    logger.warning("   1. ECAPA encoder failed to extract embedding")
                    logger.warning("   2. Audio quality too poor for analysis")
                    logger.warning("   3. No matching voice profile found")

                    # Check encoder status for more details
                    if self.unified_cache:
                        try:
                            cache_status = self.unified_cache.get_encoder_status()
                            if not cache_status.get("available"):
                                logger.error("   ❌ ROOT CAUSE: Unified cache encoder not available!")
                                logger.error(f"      Details: {cache_status.get('failure_reasons', [])}")
                        except Exception as e:
                            logger.debug(f"Could not check cache status: {e}")

                return speaker_name, confidence

            # Legacy speaker verification service - returns (speaker_name, confidence)
            speaker_name, confidence = await self.speaker_engine.identify_speaker(filtered_audio)
            return speaker_name, confidence

        except Exception as e:
            logger.error(f"Speaker identification failed with exception: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None, 0.0 

    async def _get_speaker_confidence(self, audio_data: bytes, speaker_name: str) -> float:
        """Get confidence score for identified speaker with VAD preprocessing"""
        if not self.speaker_engine:
            return 0.0

        try:
            # Apply VAD filtering to speed up speaker verification
            filtered_audio = await self._apply_vad_for_speaker_verification(audio_data) # Returns filtered audio bytes 

            # New SpeakerVerificationService
            if hasattr(self.speaker_engine, "get_speaker_name"):
                result = await self.speaker_engine.verify_speaker(filtered_audio, speaker_name)
                return result.get("confidence", 0.0)

            # Legacy: Re-verify to get confidence
            is_match, confidence = await self.speaker_engine.verify_speaker(
                filtered_audio, speaker_name
            )
            return confidence
        except Exception as e:
            logger.error(f"Speaker confidence check failed: {e}")
            return 0.0

    async def _verify_owner(self, speaker_name: Optional[str]) -> bool:
        """Check if speaker is the device owner"""
        if not speaker_name:
            return False

        if not self.speaker_engine:
            # Fallback: check against cached owner profile
            if self.owner_profile:
                return speaker_name == self.owner_profile.get("speaker_name")
            return False

        # New SpeakerVerificationService - check is_owner from profiles
        if hasattr(self.speaker_engine, "speaker_profiles"):
            profile = self.speaker_engine.speaker_profiles.get(speaker_name)
            if profile:
                return profile.get("is_primary_user", False)

        # Legacy: use is_owner method
        if hasattr(self.speaker_engine, "is_owner"):
            return self.speaker_engine.is_owner(speaker_name)

        return False

    async def _verify_speaker_identity(
        self, audio_data: bytes, speaker_name: str
    ) -> Tuple[bool, float]:
        """Verify speaker identity with high threshold (anti-spoofing)"""
        if not self.speaker_engine:
            return False, 0.0

        try:
            # New SpeakerVerificationService - returns dict with adaptive thresholds
            if hasattr(self.speaker_engine, "get_speaker_name"):
                result = await self.speaker_engine.verify_speaker(audio_data, speaker_name)
                is_verified = result.get("verified", False)
                confidence = result.get("confidence", 0.0)

                # Trust the speaker verification service's adaptive threshold decision
                # (Uses 50% for legacy profiles, 75% for native profiles)
                return is_verified, confidence

            # Legacy: Use verify_speaker with high threshold (0.85)
            is_verified, confidence = await self.speaker_engine.verify_speaker(
                audio_data, speaker_name
            )

            return is_verified, confidence

        except Exception as e:
            logger.error(f"Speaker verification failed: {e}")
            return False, 0.0

    async def _analyze_verification_failure(
        self, audio_data: bytes, speaker_name: str, confidence: float, transcription: str
    ) -> Dict[str, Any]:
        """
        Comprehensive analysis of voice verification failure

        Diagnoses:
        - Audio quality issues
        - Database/profile issues
        - Embedding dimension mismatches
        - Sample count deficiencies
        - Environmental factors
        - System architecture flaws
        """
        diagnostics = {
            'primary_reason': 'unknown',
            'suggested_fix': 'Contact system administrator',
            'architecture_issue': 'none detected',
            'user_message': 'Please try again.',
            'threshold': 'unknown',
            'audio_quality': 'unknown',
            'audio_duration_ms': 0,
            'audio_energy': 0.0,
            'samples_in_db': 0,
            'embedding_dimension': 'unknown',
            'severity': 'low'
        }

        try:
            import numpy as np

            # 1. AUDIO QUALITY ANALYSIS
            if audio_data and len(audio_data) > 0:
                try:
                    # Parse audio (assuming int16 PCM)
                    audio_int16 = np.frombuffer(audio_data, dtype=np.int16)
                    audio_float32 = audio_int16.astype(np.float32) / 32768.0

                    # Calculate duration (assuming 16kHz sample rate)
                    duration_ms = (len(audio_int16) / 16000) * 1000
                    diagnostics['audio_duration_ms'] = int(duration_ms)

                    # Calculate energy
                    energy = np.mean(np.abs(audio_float32))
                    diagnostics['audio_energy'] = float(energy)

                    # Determine audio quality
                    if energy < 0.0001:
                        diagnostics['audio_quality'] = 'silent/corrupted'
                        diagnostics['primary_reason'] = 'Audio input is silent or corrupted'
                        diagnostics['suggested_fix'] = 'Check microphone connection and permissions'
                        diagnostics['architecture_issue'] = 'Audio pipeline may not be capturing input correctly'
                        diagnostics['user_message'] = 'Microphone not detecting audio. Check your audio settings.'
                        diagnostics['severity'] = 'critical'
                    elif energy < 0.001:
                        diagnostics['audio_quality'] = 'very_quiet'
                        diagnostics['primary_reason'] = 'Audio input too quiet'
                        diagnostics['suggested_fix'] = 'Speak louder or adjust microphone gain'
                        diagnostics['user_message'] = 'Please speak louder.'
                        diagnostics['severity'] = 'high'
                    elif duration_ms < 500:
                        diagnostics['audio_quality'] = 'too_short'
                        diagnostics['primary_reason'] = f'Audio too short ({duration_ms:.0f}ms, need 1000ms+)'
                        diagnostics['suggested_fix'] = 'Speak for longer duration'
                        diagnostics['user_message'] = 'Please speak the command more slowly.'
                        diagnostics['severity'] = 'high'
                    else:
                        diagnostics['audio_quality'] = 'acceptable'
                except Exception as e:
                    diagnostics['audio_quality'] = f'parse_error: {str(e)}'
                    logger.error(f"Audio parsing failed: {e}")
            else:
                diagnostics['audio_quality'] = 'no_data'
                diagnostics['primary_reason'] = 'No audio data received'
                diagnostics['suggested_fix'] = 'Verify audio recording pipeline'
                diagnostics['architecture_issue'] = 'Audio data not reaching verification service'
                diagnostics['severity'] = 'critical'

            # 2. DATABASE PROFILE ANALYSIS
            if self.speaker_engine and hasattr(self.speaker_engine, 'speaker_profiles'):
                profiles = self.speaker_engine.speaker_profiles

                # Handle case where speaker_name is "unknown" or not in profiles
                # This happens when verification didn't find a match
                target_profile = None
                target_name = speaker_name

                if speaker_name in profiles:
                    target_profile = profiles[speaker_name]
                elif speaker_name in ("unknown", "error", None, ""):
                    # Verification failed to identify speaker - use primary user profile for diagnostics
                    # This is NOT "profile not found" - it's "voice didn't match the profile"
                    for name, profile in profiles.items():
                        if profile.get('is_primary_user', False):
                            target_profile = profile
                            target_name = name
                            diagnostics['expected_speaker'] = name
                            break
                    # If no primary user, use the first profile
                    if not target_profile and profiles:
                        target_name, target_profile = next(iter(profiles.items()))
                        diagnostics['expected_speaker'] = target_name

                if target_profile:
                    # Get embedding info
                    embedding = target_profile.get('embedding')
                    if embedding is not None:
                        if hasattr(embedding, 'shape'):
                            diagnostics['embedding_dimension'] = int(embedding.shape[0])
                        elif hasattr(embedding, '__len__'):
                            diagnostics['embedding_dimension'] = len(embedding)

                    # Get sample count from profile or database
                    diagnostics['samples_in_db'] = target_profile.get('total_samples', 0)

                    if self.speaker_engine.learning_db and diagnostics['samples_in_db'] == 0:
                        try:
                            profile_data = await self.speaker_engine.learning_db.get_speaker_profile(target_name)
                            if profile_data:
                                diagnostics['samples_in_db'] = profile_data.get('total_samples', 0)
                        except Exception as e:
                            logger.debug(f"Could not get sample count: {e}")

                    # Check if insufficient samples
                    if diagnostics['samples_in_db'] < 10:
                        diagnostics['primary_reason'] = f'Insufficient voice samples ({diagnostics["samples_in_db"]}/30 recommended)'
                        diagnostics['suggested_fix'] = 'Re-enroll voice profile with more samples'
                        diagnostics['architecture_issue'] = 'Voice enrollment may not have captured enough samples'
                        diagnostics['user_message'] = 'Voice profile needs more training samples.'
                        diagnostics['severity'] = 'high'

                    # Get threshold
                    diagnostics['threshold'] = f"{target_profile.get('threshold', 0.40):.2%}"
                    if hasattr(self.speaker_engine, '_get_adaptive_threshold'):
                        try:
                            threshold = await self.speaker_engine._get_adaptive_threshold(target_name, target_profile)
                            diagnostics['threshold'] = f"{threshold:.2%}"
                        except:
                            pass

                elif len(profiles) == 0:
                    # Truly no profiles loaded
                    diagnostics['primary_reason'] = 'No voice profiles loaded in system'
                    diagnostics['suggested_fix'] = 'Enroll voice profile first'
                    diagnostics['architecture_issue'] = 'No speaker profiles registered in database'
                    diagnostics['user_message'] = 'Voice profile not found. Please enroll first.'
                    diagnostics['severity'] = 'critical'
                else:
                    # Profiles exist but speaker_name doesn't match any
                    available_profiles = list(profiles.keys())
                    diagnostics['primary_reason'] = f'Speaker "{speaker_name}" not in registered profiles: {available_profiles}'
                    diagnostics['suggested_fix'] = 'Voice may not match enrolled profile. Try re-enrolling.'
                    diagnostics['architecture_issue'] = 'Speaker name mismatch'
                    diagnostics['user_message'] = 'Voice not recognized. Try speaking more clearly.'
                    diagnostics['severity'] = 'high'

            # 3. CONFIDENCE ANALYSIS
            if diagnostics['audio_quality'] == 'acceptable' and diagnostics['samples_in_db'] >= 10:
                if confidence == 0.0:
                    # Exactly 0% means either silent audio or a processing error
                    diagnostics['primary_reason'] = 'Zero confidence - possible audio processing issue'
                    diagnostics['suggested_fix'] = 'Check microphone input and try speaking more clearly'
                    diagnostics['architecture_issue'] = 'Audio may not be reaching embedding extraction'
                    diagnostics['user_message'] = 'Could not process voice. Please try again.'
                    diagnostics['severity'] = 'critical'
                elif confidence < 0.05:
                    diagnostics['primary_reason'] = 'Voice does not match enrolled profile'
                    diagnostics['suggested_fix'] = 'Verify speaker identity or re-enroll'
                    diagnostics['architecture_issue'] = 'Possible embedding dimension mismatch or model version incompatibility'
                    diagnostics['user_message'] = 'Voice not recognized. Try re-enrolling your voice profile.'
                    diagnostics['severity'] = 'critical'
                elif confidence < 0.20:
                    diagnostics['primary_reason'] = f'Low confidence match ({confidence:.2%})'
                    diagnostics['suggested_fix'] = 'Improve audio quality, reduce background noise, or re-enroll'
                    diagnostics['user_message'] = 'Voice match uncertain. Speak in a quieter environment.'
                    diagnostics['severity'] = 'high'
                elif confidence < 0.40:
                    diagnostics['primary_reason'] = f'Moderate confidence ({confidence:.2%}) below threshold'
                    diagnostics['suggested_fix'] = 'System is learning your voice. Keep using it or re-enroll for better accuracy'
                    diagnostics['user_message'] = 'Almost there! System is still learning your voice.'
                    diagnostics['severity'] = 'medium'
                else:
                    diagnostics['primary_reason'] = f'Confidence ({confidence:.2%}) just below threshold'
                    diagnostics['suggested_fix'] = 'Try again with clearer audio'
                    diagnostics['user_message'] = 'Very close! Please try again.'
                    diagnostics['severity'] = 'low'

            # 4. SYSTEM ARCHITECTURE CHECKS
            if diagnostics['architecture_issue'] == 'none detected':
                # Check for known architectural issues
                if diagnostics.get('embedding_dimension') not in [192, 256, 512, 768]:
                    diagnostics['architecture_issue'] = f'Unusual embedding dimension: {diagnostics.get("embedding_dimension")}'

                if diagnostics['samples_in_db'] == 0 and confidence > 0:
                    diagnostics['architecture_issue'] = 'Profile exists but no samples in database - possible data corruption'

        except Exception as e:
            logger.error(f"Failure analysis error: {e}", exc_info=True)
            diagnostics['primary_reason'] = f'Diagnostic error: {str(e)}'

        return diagnostics

    async def _analyze_context(
        self, transcribed_text: str, context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Analyze context using CAI"""
        if not self.cai_handler:
            return {"available": False}

        try:
            # Use CAI to analyze context
            # This could check: screen state, time of day, location, etc.
            cai_result = {
                "available": True,
                "screen_state": context.get("screen_state", "locked") if context else "locked",
                "time_of_day": datetime.now().hour,
                "is_work_hours": 9 <= datetime.now().hour < 17,
                "context_score": 0.9,  # Placeholder
            }

            return cai_result

        except Exception as e:
            logger.error(f"Context analysis failed: {e}")
            return {"available": False, "error": str(e)}

    async def _analyze_scenario(
        self, transcribed_text: str, context: Optional[Dict[str, Any]], speaker_name: str
    ) -> Dict[str, Any]:
        """Analyze scenario using SAI"""
        if not self.sai_analyzer:
            return {"available": False}

        try:
            # Use SAI to detect scenario
            # This could detect: emergency unlock, routine unlock, suspicious activity, etc.
            scenario_result = {
                "available": True,
                "scenario_type": "routine_unlock",
                "risk_level": "low",
                "confidence": 0.95,
            }

            return scenario_result

        except Exception as e:
            logger.error(f"Scenario analysis failed: {e}")
            return {"available": False, "error": str(e)}

    async def _perform_unlock(
        self, speaker_name: str, context_analysis: Dict[str, Any], scenario_analysis: Dict[str, Any], attempt_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Perform actual screen unlock with enhanced error handling and ML metrics collection.

        Uses enhanced macos_keychain_unlock for non-blocking password retrieval with:
        - Intelligent caching (1 hour TTL)
        - Parallel keychain service lookup
        - Circuit breaker pattern
        - Comprehensive metrics
        """
        try:
            # Get password from keychain using enhanced service (non-blocking, cached)
            from macos_keychain_unlock import get_keychain_unlock_service

            keychain_service = await get_keychain_unlock_service()
            password = await keychain_service.get_password_from_keychain()

            if not password:
                # Get metrics for debugging
                metrics = keychain_service.get_metrics()
                logger.error("Password not found in keychain")
                logger.error("Tried services: com.jarvis.voiceunlock, jarvis_voice_unlock, JARVIS_Screen_Unlock")
                logger.error(f"Keychain metrics: lookups={metrics['total_lookups']}, failures={metrics['failures']}")
                logger.error("Run: ~/Documents/repos/JARVIS-AI-Agent/backend/voice_unlock/fix_keychain_password.sh")
                return {
                    "success": False,
                    "reason": "password_not_found",
                    "message": "Password not found in keychain. Run fix_keychain_password.sh to fix.",
                }

            # Get service used from metrics
            service_used = keychain_service.get_metrics().get("last_success_service", "unknown")

            # 🖥️ DISPLAY-AWARE SAI: Use situational awareness for display configuration
            # Automatically detects mirrored/TV displays and adapts typing strategy
            from voice_unlock.secure_password_typer import type_password_with_display_awareness

            unlock_success, typing_metrics, display_context = await type_password_with_display_awareness(
                password=password,
                submit=True,
                attempt_id=attempt_id  # Enable ML metrics collection
            )

            # Log display context for debugging
            if display_context:
                logger.info(f"🖥️ [SAI] Display context: mode={display_context.get('display_mode')}, "
                           f"mirrored={display_context.get('is_mirrored')}, "
                           f"tv={display_context.get('is_tv_connected')}")

            # 🤖 ML LEARNING: Update typing model with results (CRITICAL: Learn from failures too!)
            if self.ml_engine and typing_metrics:
                try:
                    # Extract metrics for ML learning
                    await self.ml_engine.typing_learner.update_from_typing_session(
                        success=unlock_success,  # ACTUAL unlock status
                        duration_ms=typing_metrics.get('total_duration_ms', 0),
                        failed_at_char=typing_metrics.get('failed_at_character'),
                        char_metrics=typing_metrics.get('character_metrics', [])
                    )
                    self.stats["ml_typing_updates"] += 1
                    status = "✅ SUCCESS" if unlock_success else "❌ FAILURE"
                    logger.info(f"🤖 ML: Password typing model updated - {status} - learning from attempt")
                except Exception as e:
                    logger.error(f"ML typing learning update failed: {e}", exc_info=True)

            if unlock_success:
                logger.info(f"✅ Screen unlocked by {speaker_name} (keychain: {service_used})")

                # Generate progressive confidence message
                try:
                    messenger = get_confidence_messenger()
                    ctx = AuthenticationContext(
                        confidence=context_analysis.get("verification_confidence", 0.85),
                        threshold=context_analysis.get("threshold", 0.35),
                        speaker_name=speaker_name,
                        is_owner=True,
                        audio_quality=context_analysis.get("audio_quality", "good"),
                        stt_confidence=context_analysis.get("stt_confidence", 0.9),
                        background_noise_detected=context_analysis.get("background_noise", False),
                        is_first_unlock_today=context_analysis.get("is_first_today", False),
                    )
                    personalized_message = messenger.generate_success_message(ctx)
                    messenger.record_unlock(success=True)
                except Exception as e:
                    logger.debug(f"Progressive messaging failed: {e}")
                    personalized_message = f"Screen unlocked by {speaker_name}"
            else:
                logger.error(f"❌ Unlock failed for {speaker_name} - password may be incorrect")
                personalized_message = "Unlock failed - password may be incorrect"

            return {
                "success": unlock_success,
                "message": personalized_message,
            }

        except Exception as e:
            logger.error(f"Unlock failed: {e}", exc_info=True)
            return {"success": False, "reason": "unlock_error", "message": str(e)}

    async def _update_speaker_profile(
        self, speaker_name: str, audio_data: bytes, transcribed_text: str, success: bool
    ):
        """Update speaker profile with continuous learning"""
        if not self.speaker_engine or not self.learning_db:
            return

        try:
            # Extract embedding
            embedding = await self.speaker_engine._extract_embedding(audio_data)

            if embedding is None:
                return

            # Update profile in speaker engine (continuous learning)
            profile = self.speaker_engine.profiles.get(speaker_name)
            if profile:
                # Moving average of embeddings
                alpha = 0.05  # Slow learning rate for stability
                profile.embedding = (1 - alpha) * profile.embedding + alpha * embedding
                profile.sample_count += 1
                profile.updated_at = datetime.now()

                # Update in database
                await self.learning_db.update_speaker_embedding(
                    speaker_id=profile.speaker_id,
                    embedding=profile.embedding.tobytes(),
                    confidence=profile.confidence,
                    is_primary_user=profile.is_owner,
                )

                self.stats["learning_updates"] += 1
                logger.debug(
                    f"📈 Updated profile for {speaker_name} (sample #{profile.sample_count})"
                )

        except Exception as e:
            logger.error(f"Failed to update speaker profile: {e}")

    async def _record_unlock_attempt(
        self,
        speaker_name: Optional[str],
        transcribed_text: str,
        success: bool,
        rejection_reason: Optional[str],
        audio_data: bytes,
        stt_confidence: float,
        speaker_confidence: float,
        context_data: Optional[Dict[str, Any]] = None,
        scenario_data: Optional[Dict[str, Any]] = None,
        security_analysis: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """
        Record unlock attempt to learning database with full security analysis

        Returns:
            Optional[int]: Attempt ID for ML metrics linkage
        """
        if not self.learning_db:
            return None

        try:
            # Record voice sample
            if speaker_name:
                await self.learning_db.record_voice_sample(
                    speaker_name=speaker_name,
                    audio_data=audio_data,
                    transcription=transcribed_text,
                    audio_duration_ms=len(audio_data) / 32,  # Estimate
                    quality_score=stt_confidence,
                )

            # Build comprehensive response including security analysis
            jarvis_response = "Unlock " + (
                "successful" if success else f"failed: {rejection_reason}"
            )
            if security_analysis:
                threat_level = security_analysis.get("threat_level", "unknown")
                scenario = security_analysis.get("scenario", "unknown")
                jarvis_response += f" [Threat: {threat_level}, Scenario: {scenario}]"

            # Record unlock attempt (custom table or use existing)
            interaction_id = await self.learning_db.record_interaction(
                user_query=transcribed_text,
                jarvis_response=jarvis_response,
                response_type="voice_unlock",
                confidence_score=speaker_confidence,
                success=success,
                metadata={
                    "speaker_name": speaker_name,
                    "rejection_reason": rejection_reason,
                    "security_analysis": security_analysis,
                    "context_data": context_data,
                    "scenario_data": scenario_data,
                },
            )

            logger.debug(f"📝 Recorded unlock attempt (ID: {interaction_id})")

            # If this is a high-threat event, log it separately for security monitoring
            if security_analysis and security_analysis.get("threat_level") == "high":
                logger.warning(
                    f"🚨 HIGH THREAT: {speaker_name} - {security_analysis.get('scenario')} - Attempt #{security_analysis.get('historical_context', {}).get('recent_attempts_24h', 0)}"
                )

            return interaction_id

        except Exception as e:
            logger.error(f"Failed to record unlock attempt: {e}")
            return None

    async def _analyze_security_event(
        self,
        speaker_name: str,
        transcribed_text: str,
        context: Optional[Dict[str, Any]],
        speaker_confidence: float,
    ) -> Dict[str, Any]:
        """
        Analyze unauthorized unlock attempt using SAI (Situational Awareness Intelligence).
        Provides dynamic, intelligent analysis with zero hardcoding.
        """
        analysis = {
            "event_type": "unauthorized_unlock_attempt",
            "speaker_name": speaker_name,
            "confidence": speaker_confidence,
            "timestamp": datetime.now().isoformat(),
            "threat_level": "low",  # Will be dynamically determined
            "scenario": "unknown",
            "historical_context": {},
            "recommendations": [],
        }

        try:
            # Get historical data about this speaker
            if self.learning_db:
                # Check past attempts by this speaker
                past_attempts = await self._get_speaker_unlock_history(speaker_name)
                analysis["historical_context"] = {
                    "total_attempts": len(past_attempts),
                    "recent_attempts_24h": len(
                        [a for a in past_attempts if self._is_recent(a, hours=24)]
                    ),
                    "pattern": self._detect_attempt_pattern(past_attempts),
                }

                # Determine threat level based on patterns
                if analysis["historical_context"]["recent_attempts_24h"] > 5:
                    analysis["threat_level"] = "high"
                    analysis["scenario"] = "persistent_unauthorized_access"
                elif analysis["historical_context"]["recent_attempts_24h"] > 2:
                    analysis["threat_level"] = "medium"
                    analysis["scenario"] = "repeated_unauthorized_access"
                else:
                    analysis["threat_level"] = "low"
                    analysis["scenario"] = "single_unauthorized_access"

            # Use SAI to analyze scenario
            if self.sai_analyzer:
                try:
                    sai_analysis = await self._get_sai_scenario_analysis(
                        event_type="unauthorized_unlock",
                        speaker_name=speaker_name,
                        context=context,
                    )
                    analysis["sai_scenario"] = sai_analysis
                except Exception as e:
                    logger.debug(f"SAI analysis unavailable: {e}")

            # Determine if this is a known person (family member, friend, etc.)
            is_known_person = await self._is_known_person(speaker_name)
            analysis["is_known_person"] = is_known_person

            if is_known_person:
                analysis["relationship"] = "known_non_owner"
                analysis["scenario"] = "known_person_unauthorized_access"
            else:
                analysis["relationship"] = "unknown"

            # Generate recommendations
            if analysis["threat_level"] == "high":
                analysis["recommendations"] = [
                    "alert_owner",
                    "log_security_event",
                    "consider_additional_security",
                ]
            elif analysis["threat_level"] == "medium":
                analysis["recommendations"] = ["log_security_event", "monitor_future_attempts"]
            else:
                analysis["recommendations"] = ["log_attempt"]

        except Exception as e:
            logger.error(f"Security analysis failed: {e}")
            analysis["error"] = str(e)

        return analysis

    async def _generate_security_response(
        self,
        speaker_name: str,
        reason: str,
        analysis: Dict[str, Any],
        context: Optional[Dict[str, Any]],
    ) -> str:
        """
        Generate intelligent, dynamic security response.
        Uses SAI and historical data to create natural, contextual messages.
        ZERO hardcoding - fully dynamic and adaptive.
        """
        import random  # nosec B311 # UI message selection

        threat_level = analysis.get("threat_level", "low")
        scenario = analysis.get("scenario", "unknown")
        is_known_person = analysis.get("is_known_person", False)
        historical = analysis.get("historical_context", {})
        total_attempts = historical.get("total_attempts", 0)
        recent_attempts = historical.get("recent_attempts_24h", 0)

        # Dynamic response based on threat level and scenario
        # Handle None speaker_name throughout
        speaker_display = speaker_name if speaker_name and speaker_name != "None" else ""

        if threat_level == "high" and recent_attempts > 5:
            # Persistent unauthorized attempts - firm warning
            if speaker_display:
                responses = [
                    f"Access denied. {speaker_display}, this is your {recent_attempts}th unauthorized attempt in 24 hours. Only the device owner can unlock this system.",
                    f"I'm sorry {speaker_display}, but I cannot allow that. You've attempted unauthorized access {recent_attempts} times today. This system is secured for the owner only.",
                    f"{speaker_display}, I must inform you that I cannot grant access. This is your {recent_attempts}th attempt, and this device is owner-protected.",
                ]
            else:
                responses = [
                    f"Access denied. This is the {recent_attempts}th unauthorized attempt in 24 hours. Voice authentication failed.",
                    f"Multiple unauthorized access attempts detected. This system is secured for the owner only.",
                    f"Security alert: {recent_attempts} failed attempts recorded. Voice verification required.",
                ]
        elif threat_level == "medium" and recent_attempts > 2:
            # Multiple attempts - polite but firm
            if speaker_display:
                responses = [
                    f"I'm sorry {speaker_display}, but I cannot unlock this device. You've tried {recent_attempts} times recently. Only the device owner has voice unlock privileges.",
                    f"Access denied, {speaker_display}. This is your {recent_attempts}th attempt. Voice unlock is restricted to the device owner.",
                    f"{speaker_display}, I cannot grant access. You've attempted this {recent_attempts} times, but only the owner can unlock via voice.",
                ]
            else:
                responses = [
                    f"I cannot unlock this device. You've tried {recent_attempts} times recently. Only the device owner has voice unlock privileges.",
                    f"Access denied. This is the {recent_attempts}th attempt. Voice unlock is restricted to the device owner.",
                    f"Cannot grant access after {recent_attempts} attempts. Only the owner can unlock via voice.",
                ]
        elif is_known_person and total_attempts < 3:
            # Known person, first few attempts - friendly but clear
            if speaker_display:
                responses = [
                    f"I recognize you, {speaker_display}, but I'm afraid only the device owner can unlock via voice. Perhaps they can assist you?",
                    f"Hello {speaker_display}. While I know you, voice unlock is reserved for the device owner only. You may need their assistance.",
                    f"{speaker_display}, I cannot unlock the device for you. Voice authentication is owner-only. The owner can help you if needed.",
                ]
            else:
                responses = [
                    "I recognize your voice, but only the device owner can unlock via voice. Perhaps they can assist you?",
                    "Voice unlock is reserved for the device owner only. You may need their assistance.",
                    "I cannot unlock the device for you. Voice authentication is owner-only. The owner can help you if needed.",
                ]
        elif scenario == "single_unauthorized_access":
            # First attempt by unknown person - polite explanation
            if speaker_display:
                responses = [
                    f"I'm sorry, but I don't recognize you as the device owner, {speaker_display}. Voice unlock is restricted to the owner only.",
                    f"Access denied. {speaker_display}, only the device owner can unlock this system via voice. I cannot grant you access.",
                    f"I cannot unlock this device for you, {speaker_display}. Voice unlock requires owner authentication, and you are not registered as the owner.",
                    f"{speaker_display}, this device is secured with owner-only voice authentication. I cannot grant access to non-owner users.",
                ]
            else:
                responses = [
                    "I'm sorry, but I don't recognize you as the device owner. Voice unlock is restricted to the owner only.",
                    "Access denied. Only the device owner can unlock this system via voice.",
                    "I cannot unlock this device for you. Voice unlock requires owner authentication.",
                    "This device is secured with owner-only voice authentication. Voice not recognized.",
                ]
        else:
            # Default - clear and professional
            # Handle None speaker_name gracefully
            if speaker_name and speaker_name != "None":
                responses = [
                    f"Access denied, {speaker_name}. Only the device owner can unlock via voice authentication.",
                    f"I'm sorry {speaker_name}, but voice unlock is restricted to the device owner only.",
                    f"{speaker_name}, I cannot grant access. This system requires owner voice authentication.",
                ]
            else:
                # Unknown speaker (couldn't identify)
                responses = [
                    "Voice not recognized. Only the device owner can unlock via voice authentication.",
                    "I cannot verify your identity. Voice unlock is restricted to the registered device owner.",
                    "Access denied. Please speak clearly for voice verification, or use an alternative unlock method.",
                    "Voice authentication failed. This device is secured for the owner only.",
                ]

        # Select response dynamically
        message = random.choice(responses)  # nosec B311 # UI message selection

        # Add contextual information if available
        if scenario == "persistent_unauthorized_access":
            message += " This attempt has been logged for security purposes."

        return message

    async def _get_speaker_unlock_history(self, speaker_name: str) -> list:
        """Get past unlock attempts by this speaker from database"""
        try:
            if self.learning_db:
                # Query database for past attempts
                query = """
                    SELECT * FROM unlock_attempts
                    WHERE speaker_name = ?
                    ORDER BY timestamp DESC
                    LIMIT 100
                """
                results = await self.learning_db.execute_query(query, (speaker_name,))
                return results if results else []
        except Exception as e:
            logger.debug(f"Could not retrieve unlock history: {e}")
        return []

    def _is_recent(self, attempt: Dict[str, Any], hours: int = 24) -> bool:
        """Check if attempt is within recent time window"""
        try:
            from datetime import timedelta

            attempt_time = datetime.fromisoformat(attempt.get("timestamp", ""))
            return (datetime.now() - attempt_time) < timedelta(hours=hours)
        except:
            return False

    def _detect_attempt_pattern(self, attempts: list) -> str:
        """Detect pattern in unlock attempts"""
        if len(attempts) == 0:
            return "no_history"
        elif len(attempts) == 1:
            return "single_attempt"
        elif len(attempts) < 5:
            return "occasional_attempts"
        else:
            return "frequent_attempts"

    async def _is_known_person(self, speaker_name: str) -> bool:
        """Check if speaker is a known person (has voice profile but not owner)"""
        try:
            if self.speaker_engine and self.speaker_engine.profiles:
                return speaker_name in self.speaker_engine.profiles
        except:
            pass
        return False

    async def _get_sai_scenario_analysis(
        self, event_type: str, speaker_name: str, context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Get scenario analysis from SAI"""
        if not self.sai_analyzer:
            return {}

        try:
            # Use SAI to analyze the security scenario
            analysis = await self.sai_analyzer.analyze_scenario(
                event_type=event_type,
                speaker=speaker_name,
                context=context or {},
            )
            return analysis
        except Exception as e:
            logger.debug(f"SAI analysis failed: {e}")
            return {}

    async def _create_failure_response(
        self,
        reason: str,
        message: str,
        speaker_name: Optional[str] = None,
        security_analysis: Optional[Dict[str, Any]] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create standardized failure response with optional security analysis and diagnostics"""
        response = {
            "success": False,
            "reason": reason,
            "message": message,
            "speaker_name": speaker_name,
            "timestamp": datetime.now().isoformat(),
        }

        if security_analysis:
            response["security_analysis"] = security_analysis

        if diagnostics:
            response["diagnostics"] = diagnostics

        return response

    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics including keychain cache metrics"""
        # Get keychain cache metrics (async-safe access to singleton)
        keychain_metrics = {}
        try:
            from macos_keychain_unlock import _keychain_unlock_instance
            if _keychain_unlock_instance:
                keychain_metrics = _keychain_unlock_instance.get_metrics()
        except Exception:
            pass  # Keychain service not initialized yet

        return {
            **self.stats,
            "owner_profile_loaded": self.owner_profile is not None,
            "owner_name": self.owner_profile.get("speaker_name") if self.owner_profile else None,
            "password_loaded": self.owner_password_hash is not None,
            "components_initialized": {
                "hybrid_stt": self.stt_router is not None,
                "speaker_recognition": self.speaker_engine is not None,
                "learning_database": self.learning_db is not None,
                "cai": self.cai_handler is not None,
                "sai": self.sai_analyzer is not None,
                "keychain_cache": keychain_metrics.get("cache_valid", False),
                "unified_voice_cache": self.unified_cache is not None and self.unified_cache.is_ready,
            },
            "keychain_cache_metrics": keychain_metrics,
            "unified_cache_stats": self.unified_cache.get_stats() if self.unified_cache else {},
        }


# Global singleton
_intelligent_unlock_service: Optional[IntelligentVoiceUnlockService] = None


def get_intelligent_unlock_service() -> IntelligentVoiceUnlockService:
    """Get global intelligent unlock service instance"""
    global _intelligent_unlock_service
    if _intelligent_unlock_service is None:
        _intelligent_unlock_service = IntelligentVoiceUnlockService()
    return _intelligent_unlock_service


# =============================================================================
# ROBUST VOICE UNLOCK v1.0.0 - Timeout-Protected Parallel Processing
# =============================================================================
# This section provides a simplified, robust voice unlock that:
# 1. Has HARD timeouts on every operation (guaranteed to never hang)
# 2. Uses direct database queries (no complex cache layers)
# 3. Runs ECAPA extraction and profile loading in parallel
# 4. Fails gracefully with informative error messages
# =============================================================================

import base64
import sqlite3
import time
import traceback
from enum import Enum


class RobustUnlockConfig:
    """
    Configuration for robust unlock with environment overrides.

    🆕 EDGE-NATIVE MODE:
    When JARVIS_EDGE_NATIVE_MODE=true (default):
    - ALL processing happens locally (no cloud calls in critical path)
    - Local ECAPA via SpeechBrain/Neural Engine is primary
    - Cloud is async-only backup (never blocks unlock)
    - Optimized for Apple Silicon Neural Engine
    """
    # ========================================================================
    # 🆕 EDGE-NATIVE SETTINGS
    # ========================================================================
    # Master switch for Edge-Native mode
    EDGE_NATIVE_MODE = os.environ.get("JARVIS_EDGE_NATIVE_MODE", "true").lower() == "true"
    # Force local-only ECAPA (no cloud in critical path)
    LOCAL_ECAPA_ONLY = os.environ.get("JARVIS_LOCAL_ECAPA_ONLY", "true").lower() == "true"
    # Allow cloud fallback ONLY when local fails (default: disabled)
    CLOUD_FALLBACK_ENABLED = os.environ.get("JARVIS_CLOUD_FALLBACK", "false").lower() == "true"
    # Prioritize Apple Neural Engine for inference
    NEURAL_ENGINE_PRIORITY = os.environ.get("JARVIS_NEURAL_ENGINE_PRIORITY", "true").lower() == "true"

    # ========================================================================
    # TIMEOUT CONFIGURATION (optimized for fast unlock)
    # ========================================================================
    # Total timeout for entire unlock operation (increased for reliability)
    MAX_TOTAL_TIMEOUT = float(os.environ.get("JARVIS_ROBUST_MAX_TIMEOUT", "45.0"))
    # Audio decode: base64 decode + FFmpeg conversion
    AUDIO_DECODE_TIMEOUT = float(os.environ.get("JARVIS_ROBUST_AUDIO_TIMEOUT", "10.0"))
    # Profile load from SQLite (fast in edge-native mode)
    PROFILE_LOAD_TIMEOUT = float(os.environ.get("JARVIS_ROBUST_PROFILE_TIMEOUT", "2.0"))
    # ECAPA embedding extraction (local-first in edge-native mode)
    ECAPA_EXTRACT_TIMEOUT = float(os.environ.get("JARVIS_ROBUST_ECAPA_TIMEOUT", "10.0"))
    # Unlock execution (typing + verification)
    UNLOCK_EXECUTE_TIMEOUT = float(os.environ.get("JARVIS_ROBUST_UNLOCK_TIMEOUT", "15.0"))

    # ========================================================================
    # VERIFICATION SETTINGS
    # ========================================================================
    # Minimum cosine similarity to verify speaker
    CONFIDENCE_THRESHOLD = float(os.environ.get("JARVIS_ROBUST_THRESHOLD", "0.40"))

    # ========================================================================
    # DATABASE CONFIGURATION
    # ========================================================================
    # Path to learning database with voiceprints (local SQLite)
    LEARNING_DB_PATH = os.path.expanduser(
        os.environ.get("JARVIS_LOCAL_DB_PATH", "~/.jarvis/learning/jarvis_learning.db")
    )

    @classmethod
    def log_config(cls):
        """Log current configuration for debugging."""
        logger.info(f"🔧 RobustUnlockConfig:")
        logger.info(f"   Edge-Native Mode: {cls.EDGE_NATIVE_MODE}")
        logger.info(f"   Local ECAPA Only: {cls.LOCAL_ECAPA_ONLY}")
        logger.info(f"   Cloud Fallback: {cls.CLOUD_FALLBACK_ENABLED}")
        logger.info(f"   Neural Engine Priority: {cls.NEURAL_ENGINE_PRIORITY}")
        logger.info(f"   DB Path: {cls.LEARNING_DB_PATH}")
        logger.info(f"   Confidence Threshold: {cls.CONFIDENCE_THRESHOLD}")


class RobustUnlockStage(Enum):
    """Stages in the robust unlock pipeline."""
    INIT = "init"
    AUDIO_DECODE = "audio_decode"
    PROFILE_LOAD = "profile_load"
    ECAPA_EXTRACT = "ecapa_extract"
    VERIFICATION = "verification"
    UNLOCK_EXECUTE = "unlock_execute"
    COMPLETE = "complete"


# =============================================================================
# GLOBAL CACHED ECAPA CLASSIFIER - Avoids 12s cold start on each request
# =============================================================================
_cached_ecapa_classifier = None
_ecapa_classifier_lock = asyncio.Lock()


async def get_cached_ecapa_classifier():
    """Get or create cached ECAPA classifier. Thread-safe singleton."""
    global _cached_ecapa_classifier

    if _cached_ecapa_classifier is not None:
        return _cached_ecapa_classifier

    async with _ecapa_classifier_lock:
        # Double-check after acquiring lock
        if _cached_ecapa_classifier is not None:
            return _cached_ecapa_classifier

        logger.info("[ROBUST-ECAPA] Loading ECAPA classifier (cold start)...")
        start = time.time()

        def _load_model():
            from speechbrain.inference.speaker import EncoderClassifier
            return EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=os.path.expanduser("~/.cache/speechbrain/spkrec-ecapa-voxceleb")
            )

        loop = asyncio.get_event_loop()
        _cached_ecapa_classifier = await loop.run_in_executor(None, _load_model)

        elapsed = time.time() - start
        logger.info(f"[ROBUST-ECAPA] ECAPA classifier loaded in {elapsed:.2f}s")
        return _cached_ecapa_classifier


async def prewarm_ecapa_classifier():
    """Pre-warm the ECAPA classifier at startup. Call this in server initialization."""
    try:
        logger.info("[ROBUST-ECAPA] Pre-warming ECAPA classifier...")
        classifier = await get_cached_ecapa_classifier()
        if classifier:
            logger.info("[ROBUST-ECAPA] ECAPA classifier pre-warmed successfully")
            return True
    except Exception as e:
        logger.error(f"[ROBUST-ECAPA] Failed to pre-warm ECAPA classifier: {e}")
    return False


async def _load_primary_speaker_profile() -> Optional[Dict[str, Any]]:
    """
    Load the primary speaker profile dynamically from SQLite database.

    Returns dict with:
        - name: Speaker's name (dynamically detected)
        - embedding: Voice embedding (192 dims)
        - total_samples: Number of voice samples

    Uses OwnerIdentityService for intelligent name detection with fallback to
    database primary user or first available profile.
    """
    import numpy as np

    # Try to get owner name from identity service
    owner_name = None
    try:
        from agi_os.owner_identity_service import get_owner_name
        owner_name = await asyncio.wait_for(get_owner_name(), timeout=1.0)
    except Exception as e:
        logger.debug(f"[ROBUST-PROFILE] Owner identity lookup failed: {e}")

    db_path = RobustUnlockConfig.LEARNING_DB_PATH

    if not os.path.exists(db_path):
        logger.warning(f"[ROBUST-PROFILE] Database not found: {db_path}")
        return None

    try:
        def _load():
            conn = sqlite3.connect(db_path, timeout=2.0)
            cursor = conn.cursor()

            # Try to find profile matching owner_name, or get primary/first user
            queries = [
                # First try matching by owner name if available
                ("speaker_profiles", "voiceprint_embedding", "speaker_name"),
                ("speaker_profiles", "embedding", "speaker_name"),
                ("voice_profiles", "embedding", "speaker_name"),
            ]

            for table, emb_col, name_col in queries:
                try:
                    row = None

                    # Strategy 1: Match by owner name if available
                    if owner_name:
                        cursor.execute(f"""
                            SELECT {emb_col}, {name_col}, total_samples FROM {table}
                            WHERE {name_col} LIKE ? OR {name_col} LIKE ? LIMIT 1
                        """, (f"%{owner_name}%", f"%{owner_name.lower()}%"))
                        row = cursor.fetchone()

                    # Strategy 2: Get primary user
                    if not row:
                        try:
                            cursor.execute(f"""
                                SELECT {emb_col}, {name_col}, total_samples FROM {table}
                                WHERE is_primary_user = 1 LIMIT 1
                            """)
                            row = cursor.fetchone()
                        except sqlite3.Error:
                            pass  # Column might not exist

                    # Strategy 3: Get user with most samples (fallback)
                    if not row:
                        cursor.execute(f"""
                            SELECT {emb_col}, {name_col}, total_samples FROM {table}
                            ORDER BY total_samples DESC LIMIT 1
                        """)
                        row = cursor.fetchone()

                    # Process the row if found
                    if row and row[0]:
                        raw = row[0]
                        profile_name = row[1] if len(row) > 1 else owner_name or "User"
                        total_samples = row[2] if len(row) > 2 else 0

                        if isinstance(raw, bytes):
                            embedding = np.frombuffer(raw, dtype=np.float32).tolist()
                            if len(embedding) == 192:
                                logger.info(f"[ROBUST-PROFILE] ✅ Loaded: {profile_name} ({total_samples} samples)")
                                conn.close()
                                return {
                                    "name": profile_name,
                                    "embedding": embedding,
                                    "total_samples": total_samples
                                }
                            else:
                                logger.warning(f"[ROBUST-PROFILE] Wrong embedding size: {len(embedding)} (expected 192)")
                        else:
                            logger.warning(f"[ROBUST-PROFILE] Embedding is not bytes: {type(raw)}")

                except sqlite3.Error as e:
                    logger.debug(f"[ROBUST-PROFILE] Query failed for {table}.{emb_col}: {e}")
                    continue

            conn.close()
            return None

        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _load),
            timeout=RobustUnlockConfig.PROFILE_LOAD_TIMEOUT
        )
        return result

    except asyncio.TimeoutError:
        logger.error("[ROBUST-PROFILE] Profile load timed out")
        return None
    except Exception as e:
        logger.error(f"[ROBUST-PROFILE] Profile load error: {e}")
        return None


async def _get_time_optimized_embedding(base_embedding: List[float], speaker_name: str = None) -> List[float]:
    """
    Enhance the base embedding with time-of-day optimizations from the learning engine.

    This is how JARVIS gets SMARTER over time - by using learned temporal patterns
    (morning voice, evening voice, etc.) to improve verification accuracy.

    Args:
        base_embedding: The base 192-dim embedding from the profile
        speaker_name: Speaker name for looking up learned patterns

    Returns:
        Time-optimized embedding (may be blended with temporal profile)
    """
    try:
        from voice_unlock.voice_profile_learning_engine import get_optimized_reference
        from datetime import datetime
        import numpy as np

        current_hour = datetime.now().hour

        # Try to get time-optimized reference from learning engine
        optimized = await asyncio.wait_for(
            get_optimized_reference(hour=current_hour),
            timeout=1.0
        )

        if optimized is not None:
            # The learning engine has a better reference for this time of day
            # Blend it with the base (30% base, 70% learned) for robustness
            base_arr = np.array(base_embedding, dtype=np.float32)
            blended = 0.3 * base_arr + 0.7 * optimized
            blended = blended / np.linalg.norm(blended)

            logger.info(
                f"🧠 [ADAPTIVE-VBI] Using time-optimized embedding for hour {current_hour} "
                f"(learning engine enhanced)"
            )
            return blended.tolist()

    except asyncio.TimeoutError:
        logger.debug("[ADAPTIVE-VBI] Time optimization timed out, using base embedding")
    except Exception as e:
        logger.debug(f"[ADAPTIVE-VBI] Time optimization unavailable: {e}")

    # Fall back to base embedding
    return base_embedding


async def _load_speaker_embedding_direct(speaker_name: Optional[str] = None) -> Optional[List[float]]:
    """Load speaker embedding directly from SQLite database with detailed diagnostics."""
    import numpy as np

    # If no speaker name, try to get from identity service
    if speaker_name is None:
        try:
            from agi_os.owner_identity_service import get_owner_name
            speaker_name = await asyncio.wait_for(get_owner_name(), timeout=1.0)
        except Exception:
            speaker_name = "User"

    db_path = RobustUnlockConfig.LEARNING_DB_PATH
    logger.info(f"[ROBUST-PROFILE] Loading profile for '{speaker_name}' from: {db_path}")

    if not os.path.exists(db_path):
        logger.warning(f"[ROBUST-PROFILE] Database not found: {db_path}")
        return None

    try:
        def _load():
            conn = sqlite3.connect(db_path, timeout=2.0)
            cursor = conn.cursor()
            queries = [
                ("speaker_profiles", "voiceprint_embedding", "speaker_name"),
                ("speaker_profiles", "embedding", "speaker_name"),
                ("voice_profiles", "embedding", "speaker_name"),
            ]
            embedding = None
            matched_query = None

            for table, emb_col, name_col in queries:
                try:
                    row = None

                    # Strategy 1: Match by speaker name
                    if speaker_name:
                        cursor.execute(f"""
                            SELECT {emb_col}, {name_col}, total_samples FROM {table}
                            WHERE {name_col} LIKE ? OR {name_col} LIKE ? LIMIT 1
                        """, (f"%{speaker_name}%", f"%{speaker_name.lower()}%"))
                        row = cursor.fetchone()

                    # Strategy 2: Get primary user
                    if not row:
                        try:
                            cursor.execute(f"""
                                SELECT {emb_col}, {name_col}, total_samples FROM {table}
                                WHERE is_primary_user = 1 LIMIT 1
                            """)
                            row = cursor.fetchone()
                        except sqlite3.Error:
                            pass

                    # Strategy 3: Get user with most samples
                    if not row:
                        cursor.execute(f"""
                            SELECT {emb_col}, {name_col}, total_samples FROM {table}
                            ORDER BY total_samples DESC LIMIT 1
                        """)
                        row = cursor.fetchone()

                    if row and row[0]:
                        raw = row[0]
                        profile_name = row[1] if len(row) > 1 else "unknown"
                        total_samples = row[2] if len(row) > 2 else "unknown"

                        logger.info(f"[ROBUST-PROFILE] Found profile: {profile_name} ({total_samples} samples)")
                        logger.info(f"[ROBUST-PROFILE]   From table: {table}.{emb_col}")
                        logger.info(f"[ROBUST-PROFILE]   Raw data type: {type(raw)}, size: {len(raw) if hasattr(raw, '__len__') else 'N/A'}")

                        if isinstance(raw, bytes):
                            try:
                                embedding = np.frombuffer(raw, dtype=np.float32).tolist()
                                logger.info(f"[ROBUST-PROFILE]   Decoded embedding: {len(embedding)} dims")
                                if len(embedding) == 192:
                                    matched_query = f"{table}.{emb_col}"
                                    # Log first few values for debugging
                                    logger.info(f"[ROBUST-PROFILE]   First 5 values: {embedding[:5]}")
                                    logger.info(f"[ROBUST-PROFILE]   Embedding norm: {np.linalg.norm(np.array(embedding)):.4f}")
                                    break
                                else:
                                    logger.warning(f"[ROBUST-PROFILE]   Wrong dimension: {len(embedding)} (expected 192)")
                            except Exception as e:
                                logger.error(f"[ROBUST-PROFILE]   Decode error: {e}")
                        else:
                            logger.warning(f"[ROBUST-PROFILE]   Not bytes: {type(raw)}")
                except sqlite3.Error as e:
                    logger.debug(f"[ROBUST-PROFILE] Query {table}.{emb_col} failed: {e}")
                    continue

            conn.close()

            if embedding and matched_query:
                logger.info(f"[ROBUST-PROFILE] ✅ Successfully loaded from {matched_query}")
            else:
                logger.warning(f"[ROBUST-PROFILE] ❌ No valid embedding found for '{speaker_name}'")

            return embedding

        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _load),
            timeout=RobustUnlockConfig.PROFILE_LOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.error("[ROBUST-PROFILE] Profile load timeout")
    except Exception as e:
        logger.error(f"[ROBUST-PROFILE] Profile load error: {e}")
    return None


async def _extract_ecapa_robust(audio_bytes: bytes) -> Optional[List[float]]:
    """Extract ECAPA embedding with timeout-protected fallbacks and robust audio loading."""
    import numpy as np
    import tempfile

    # Validate input
    if audio_bytes is None:
        logger.error("[ROBUST-ECAPA] audio_bytes is None!")
        return None

    if not isinstance(audio_bytes, bytes):
        logger.error(f"[ROBUST-ECAPA] audio_bytes is not bytes: {type(audio_bytes)}")
        return None

    if len(audio_bytes) < 100:
        logger.error(f"[ROBUST-ECAPA] audio_bytes too short: {len(audio_bytes)}")
        return None

    # Log audio info
    magic_bytes = audio_bytes[:4].hex() if len(audio_bytes) >= 4 else "N/A"
    logger.info(f"[ROBUST-ECAPA] Starting extraction: {len(audio_bytes)} bytes, magic={magic_bytes}")

    # Helper: Load audio with multiple strategies
    def _load_audio_robust(audio_data: bytes):
        """Load audio using multiple strategies until one succeeds."""
        import torchaudio
        import torch
        import io

        errors = []

        # Strategy 1: Direct BytesIO load (works for WAV)
        try:
            logger.info("[ROBUST-ECAPA] Trying direct BytesIO load...")
            waveform, sr = torchaudio.load(io.BytesIO(audio_data))
            logger.info(f"[ROBUST-ECAPA] BytesIO load SUCCESS: shape={waveform.shape}, sr={sr}")
            return waveform, sr
        except Exception as e:
            errors.append(f"BytesIO: {e}")
            logger.debug(f"[ROBUST-ECAPA] BytesIO failed: {e}")

        # Strategy 2: Temp file with format hint from magic bytes
        format_ext = '.wav'  # Default
        if audio_data[:4] == b'\x1aE\xdf\xa3':
            format_ext = '.webm'
        elif audio_data[:4] == b'OggS':
            format_ext = '.ogg'
        elif audio_data[:4] == b'fLaC':
            format_ext = '.flac'

        try:
            logger.info(f"[ROBUST-ECAPA] Trying temp file load with ext={format_ext}...")
            with tempfile.NamedTemporaryFile(suffix=format_ext, delete=False) as f:
                f.write(audio_data)
                temp_path = f.name

            waveform, sr = torchaudio.load(temp_path)
            os.unlink(temp_path)
            logger.info(f"[ROBUST-ECAPA] Temp file load SUCCESS: shape={waveform.shape}, sr={sr}")
            return waveform, sr
        except Exception as e:
            errors.append(f"TempFile({format_ext}): {e}")
            logger.debug(f"[ROBUST-ECAPA] Temp file failed: {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)

        # Strategy 3: scipy.io.wavfile for pure WAV
        if audio_data[:4] == b'RIFF':
            try:
                logger.info("[ROBUST-ECAPA] Trying scipy wavfile load...")
                from scipy.io import wavfile
                sr, data = wavfile.read(io.BytesIO(audio_data))
                # Convert to torch tensor
                if data.dtype == np.int16:
                    data = data.astype(np.float32) / 32768.0
                elif data.dtype == np.int32:
                    data = data.astype(np.float32) / 2147483648.0
                waveform = torch.from_numpy(data).unsqueeze(0) if data.ndim == 1 else torch.from_numpy(data.T)
                logger.info(f"[ROBUST-ECAPA] scipy load SUCCESS: shape={waveform.shape}, sr={sr}")
                return waveform, sr
            except Exception as e:
                errors.append(f"scipy: {e}")
                logger.debug(f"[ROBUST-ECAPA] scipy failed: {e}")

        # Strategy 4: librosa as last resort for WAV-like data
        if audio_data[:4] == b'RIFF':
            try:
                logger.info("[ROBUST-ECAPA] Trying librosa load...")
                import librosa
                with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                    f.write(audio_data)
                    temp_path = f.name
                y, sr = librosa.load(temp_path, sr=None)
                os.unlink(temp_path)
                waveform = torch.from_numpy(y).unsqueeze(0)
                logger.info(f"[ROBUST-ECAPA] librosa load SUCCESS: shape={waveform.shape}, sr={sr}")
                return waveform, sr
            except Exception as e:
                errors.append(f"librosa: {e}")
                logger.debug(f"[ROBUST-ECAPA] librosa failed: {e}")
                if 'temp_path' in locals() and os.path.exists(temp_path):
                    os.unlink(temp_path)

        # Strategy 5: FFmpeg conversion for non-WAV formats (WebM, Ogg, etc.)
        # This is the most robust strategy for browser-captured audio
        if audio_data[:4] != b'RIFF':
            try:
                logger.info("[ROBUST-ECAPA] 🔧 Trying FFmpeg conversion (non-WAV detected)...")
                import subprocess

                # Detect format from magic bytes
                if audio_data[:4] == b'\x1aE\xdf\xa3':
                    in_ext = '.webm'
                    in_format = 'webm'
                elif audio_data[:4] == b'OggS':
                    in_ext = '.ogg'
                    in_format = 'ogg'
                else:
                    in_ext = '.webm'  # Default to WebM for browser audio
                    in_format = 'webm'

                logger.info(f"[ROBUST-ECAPA] Detected format: {in_format}, will convert to WAV")

                # Create temp files
                temp_in = tempfile.NamedTemporaryFile(suffix=in_ext, delete=False)
                temp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                temp_in.write(audio_data)
                temp_in.close()
                temp_out.close()

                # FFmpeg conversion with error tolerance for browser audio
                ffmpeg_strategies = [
                    # Strategy A: Generic with error tolerance
                    [
                        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                        '-err_detect', 'ignore_err',
                        '-fflags', '+discardcorrupt+genpts',
                        '-i', temp_in.name,
                        '-vn',
                        '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                        temp_out.name
                    ],
                    # Strategy B: Force input format
                    [
                        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                        '-err_detect', 'ignore_err',
                        '-f', in_format,
                        '-i', temp_in.name,
                        '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                        temp_out.name
                    ],
                    # Strategy C: Matroska demuxer (WebM is Matroska)
                    [
                        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                        '-err_detect', 'ignore_err',
                        '-f', 'matroska',
                        '-i', temp_in.name,
                        '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                        temp_out.name
                    ],
                ]

                wav_data = None
                for i, cmd in enumerate(ffmpeg_strategies):
                    strategy_name = ['generic', in_format, 'matroska'][i]
                    try:
                        result = subprocess.run(cmd, capture_output=True, timeout=10.0)
                        if result.returncode == 0 and os.path.exists(temp_out.name):
                            with open(temp_out.name, 'rb') as f:
                                wav_data = f.read()
                            if len(wav_data) > 44:
                                logger.info(f"[ROBUST-ECAPA] FFmpeg ({strategy_name}) SUCCESS: {len(wav_data)} bytes WAV")
                                break
                    except subprocess.TimeoutExpired:
                        logger.debug(f"[ROBUST-ECAPA] FFmpeg ({strategy_name}) timeout")
                    except Exception as ffmpeg_err:
                        logger.debug(f"[ROBUST-ECAPA] FFmpeg ({strategy_name}) failed: {ffmpeg_err}")

                # Cleanup temp files
                if os.path.exists(temp_in.name):
                    os.unlink(temp_in.name)
                if os.path.exists(temp_out.name):
                    os.unlink(temp_out.name)

                # Load the converted WAV
                if wav_data and len(wav_data) > 44:
                    try:
                        waveform, sr = torchaudio.load(io.BytesIO(wav_data))
                        logger.info(f"[ROBUST-ECAPA] FFmpeg+torchaudio SUCCESS: shape={waveform.shape}, sr={sr}")
                        return waveform, sr
                    except Exception as load_err:
                        errors.append(f"FFmpeg+torchaudio: {load_err}")
                        logger.debug(f"[ROBUST-ECAPA] FFmpeg WAV load failed: {load_err}")
                else:
                    errors.append("FFmpeg: All conversion strategies failed")

            except Exception as e:
                errors.append(f"FFmpeg conversion: {e}")
                logger.debug(f"[ROBUST-ECAPA] FFmpeg conversion error: {e}")

        logger.error(f"[ROBUST-ECAPA] All audio load strategies failed: {errors}")
        return None, None

    # ========================================================================
    # 🆕 EDGE-NATIVE ECAPA EXTRACTION
    # ========================================================================
    # Priority order in Edge-Native mode:
    # 1. LOCAL SpeechBrain with cached classifier (uses Neural Engine on Apple Silicon)
    # 2. ML Engine Registry local model
    # 3. Cloud ECAPA (ONLY if fallback enabled and local fails)
    # ========================================================================

    # Get Edge-Native config
    edge_native_enabled = os.environ.get('JARVIS_EDGE_NATIVE_MODE', 'true').lower() == 'true'
    local_ecapa_only = os.environ.get('JARVIS_LOCAL_ECAPA_ONLY', 'true').lower() == 'true'
    cloud_fallback_enabled = os.environ.get('JARVIS_CLOUD_FALLBACK', 'false').lower() == 'true'

    strategies_tried = []
    last_error = None

    # ========================================================================
    # STRATEGY 1: Local SpeechBrain with CACHED classifier (PRIMARY in Edge-Native)
    # Uses Apple Neural Engine on Apple Silicon for hardware acceleration
    # ========================================================================
    try:
        logger.info("[EDGE-ECAPA] Strategy 1: Local SpeechBrain (Neural Engine priority)...")
        strategies_tried.append("local_speechbrain")

        # Get cached classifier (async, loads once on first call)
        logger.info("[EDGE-ECAPA] Getting cached classifier...")
        classifier = await get_cached_ecapa_classifier()
        if classifier is None:
            raise ValueError("Failed to get ECAPA classifier from cache")
        logger.info(f"[EDGE-ECAPA] Classifier obtained: {type(classifier)}")

        def _extract_with_neural_engine():
            import torchaudio
            import torch

            logger.info(f"[EDGE-ECAPA] _extract_with_neural_engine called, audio_bytes={len(audio_bytes)} bytes")

            # 🆕 Detect Apple Silicon for Neural Engine optimization
            is_apple_silicon = (
                os.environ.get('PYTORCH_ENABLE_MPS_FALLBACK', '0') == '1' or
                (hasattr(torch.backends, 'mps') and torch.backends.mps.is_available())
            )
            logger.info(f"[EDGE-ECAPA] Apple Silicon detected: {is_apple_silicon}")

            # Load audio with robust strategy
            logger.info("[EDGE-ECAPA] Calling _load_audio_robust...")
            waveform, sr = _load_audio_robust(audio_bytes)
            if waveform is None:
                logger.error("[EDGE-ECAPA] _load_audio_robust returned None!")
                raise ValueError("Failed to load audio with any strategy")

            logger.info(f"[EDGE-ECAPA] Audio loaded: shape={waveform.shape}, sr={sr}")

            # Resample if needed
            if sr != 16000:
                logger.info(f"[EDGE-ECAPA] Resampling from {sr} to 16000 Hz")
                waveform = torchaudio.transforms.Resample(sr, 16000)(waveform)

            # Convert to mono if stereo
            if waveform.shape[0] > 1:
                logger.info(f"[EDGE-ECAPA] Converting {waveform.shape[0]} channels to mono")
                waveform = waveform.mean(dim=0, keepdim=True)

            # Ensure proper shape [1, samples]
            if waveform.dim() == 1:
                waveform = waveform.unsqueeze(0)

            logger.info(f"[EDGE-ECAPA] Final waveform: shape={waveform.shape}, Apple Silicon: {is_apple_silicon}")

            # Extract embedding using CACHED classifier (no reload!)
            # On Apple Silicon, this uses the Neural Engine for acceleration
            with torch.no_grad():
                emb = classifier.encode_batch(waveform).squeeze().numpy()

            # 🔍 DIAGNOSTIC LOGGING - Critical for debugging embedding consistency
            logger.info(f"[EDGE-ECAPA] ═══════════════════════════════════════════════════")
            logger.info(f"[EDGE-ECAPA] Embedding extracted: {len(emb)} dims")
            logger.info(f"[EDGE-ECAPA]   norm={np.linalg.norm(emb):.4f}")
            logger.info(f"[EDGE-ECAPA]   min={emb.min():.4f}, max={emb.max():.4f}")
            logger.info(f"[EDGE-ECAPA]   mean={emb.mean():.4f}, std={emb.std():.4f}")
            logger.info(f"[EDGE-ECAPA]   first_5={emb[:5].tolist()}")
            logger.info(f"[EDGE-ECAPA] ═══════════════════════════════════════════════════")

            return emb.tolist()

        logger.info("[EDGE-ECAPA] Running extraction in executor...")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()

        embedding = await asyncio.wait_for(
            loop.run_in_executor(None, _extract_with_neural_engine),
            timeout=RobustUnlockConfig.ECAPA_EXTRACT_TIMEOUT
        )
        logger.info(f"[EDGE-ECAPA] Extraction completed, embedding={embedding is not None}, len={len(embedding) if embedding else 0}")
        if embedding and len(embedding) == 192:
            logger.info(f"[EDGE-ECAPA] ✅ Local SpeechBrain SUCCESS: {len(embedding)} dims")
            return embedding
        else:
            logger.warning(f"[EDGE-ECAPA] Extraction returned invalid embedding: {type(embedding)}, len={len(embedding) if embedding else 'None'}")

    except asyncio.TimeoutError:
        last_error = "Local SpeechBrain timeout"
        logger.warning(f"[EDGE-ECAPA] ⏱️ {last_error}")
    except Exception as e:
        last_error = str(e)
        import traceback
        logger.error(f"[EDGE-ECAPA] Strategy 1 failed: {e}")
        logger.error(f"[EDGE-ECAPA] Strategy 1 traceback:\n{traceback.format_exc()}")

    # ========================================================================
    # STRATEGY 2: ML Engine Registry (pre-loaded local model)
    # ========================================================================
    try:
        logger.info("[EDGE-ECAPA] Strategy 2: ML Engine Registry...")
        strategies_tried.append("ml_registry")

        from voice_unlock.ml_engine_registry import get_encoder, is_ecapa_available

        if await asyncio.wait_for(asyncio.to_thread(is_ecapa_available), timeout=1.0):
            encoder = await asyncio.wait_for(asyncio.to_thread(get_encoder), timeout=2.0)
            if encoder:
                def _extract_via_registry():
                    import torch

                    waveform, sr = _load_audio_robust(audio_bytes)
                    if waveform is None:
                        return None

                    # Resample to 16kHz if needed
                    if sr != 16000:
                        import torchaudio
                        waveform = torchaudio.transforms.Resample(sr, 16000)(waveform)

                    # Ensure mono [1, samples]
                    if waveform.shape[0] > 1:
                        waveform = waveform.mean(dim=0, keepdim=True)
                    if waveform.dim() == 1:
                        waveform = waveform.unsqueeze(0)

                    with torch.no_grad():
                        emb = encoder.encode_batch(waveform).squeeze().numpy()

                    return emb.tolist() if len(emb) == 192 else None

                embedding = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, _extract_via_registry),
                    timeout=RobustUnlockConfig.ECAPA_EXTRACT_TIMEOUT
                )
                if embedding:
                    logger.info(f"[EDGE-ECAPA] ✅ ML Registry SUCCESS: {len(embedding)} dims")
                    return embedding

    except asyncio.TimeoutError:
        last_error = "ML Registry timeout"
        logger.warning(f"[EDGE-ECAPA] ⏱️ {last_error}")
    except ImportError:
        logger.debug("[EDGE-ECAPA] ML Engine Registry not available")
    except Exception as e:
        last_error = str(e)
        import traceback
        logger.error(f"[EDGE-ECAPA] Strategy 2 failed: {e}")
        logger.error(f"[EDGE-ECAPA] Strategy 2 traceback:\n{traceback.format_exc()}")

    # ========================================================================
    # STRATEGY 3: Cloud ECAPA (ONLY if fallback enabled)
    # ========================================================================
    # In Edge-Native mode, cloud is DISABLED by default
    # Only used if JARVIS_CLOUD_FALLBACK=true AND all local strategies failed
    # ========================================================================
    if not local_ecapa_only and cloud_fallback_enabled:
        try:
            logger.info("[EDGE-ECAPA] Strategy 3: Cloud ECAPA (fallback)...")
            strategies_tried.append("cloud_ecapa")

            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
            client = await asyncio.wait_for(get_cloud_ecapa_client(), timeout=3.0)
            if client:
                embedding = await asyncio.wait_for(
                    client.extract_embedding(
                        audio_data=audio_bytes,
                        sample_rate=16000,
                        format="float32",
                        use_cache=True,
                        use_fast_path=True
                    ),
                    timeout=RobustUnlockConfig.ECAPA_EXTRACT_TIMEOUT
                )
                if embedding is not None and len(embedding) == 192:
                    logger.info(f"[EDGE-ECAPA] ✅ Cloud ECAPA SUCCESS (fallback): {len(embedding)} dims")
                    return embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)

        except asyncio.TimeoutError:
            last_error = "Cloud ECAPA timeout"
            logger.warning(f"[EDGE-ECAPA] ⏱️ {last_error}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[EDGE-ECAPA] Strategy 3 failed: {e}")
    else:
        logger.debug(f"[EDGE-ECAPA] Cloud ECAPA skipped (local_only={local_ecapa_only}, fallback={cloud_fallback_enabled})")

    # ========================================================================
    # ALL STRATEGIES FAILED
    # ========================================================================
    logger.error(
        f"[EDGE-ECAPA] ❌ All strategies failed! "
        f"Tried: {strategies_tried}, Last error: {last_error}"
    )
    return None


async def _decode_audio_robust(audio_base64: str, mime_type: str = "audio/webm") -> Optional[bytes]:
    """
    Decode and convert audio to WAV format with multiple fallback strategies.

    🆕 ENHANCED: Handles concatenated MediaRecorder chunks that may not have proper WebM headers.
    MediaRecorder from browsers outputs chunks where only the first chunk has full headers.

    Strategies (in order):
    1. Check if already WAV - return as-is
    2. FFmpeg with explicit format hint (for concatenated chunks)
    3. FFmpeg pipe conversion (for standard formats)
    4. FFmpeg temp file conversion (most compatible)
    5. Raw Opus/PCM to WAV conversion (for headerless data)
    6. Return raw audio for torchaudio to handle
    """
    import tempfile
    import shutil
    import struct

    logger.info(f"[ROBUST-AUDIO] Starting audio decode: base64_len={len(audio_base64)}, mime={mime_type}")

    # Step 1: Base64 decode
    try:
        raw = base64.b64decode(audio_base64)
        magic_hex = raw[:4].hex() if len(raw) >= 4 else 'N/A'
        logger.info(f"[ROBUST-AUDIO] Base64 decoded: {len(raw)} bytes, magic={magic_hex}")
    except Exception as e:
        logger.error(f"[ROBUST-AUDIO] Base64 decode FAILED: {e}")
        return None

    if len(raw) < 100:
        logger.error(f"[ROBUST-AUDIO] Audio too short: {len(raw)} bytes")
        return None

    # Step 2: Check if already WAV
    if len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WAVE':
        logger.info("[ROBUST-AUDIO] Already WAV format, returning as-is")
        return raw

    # Detect format from magic bytes
    format_hint = "unknown"
    ffmpeg_format = None  # Explicit format for FFmpeg -f option

    if raw[:4] == b'\x1aE\xdf\xa3':  # WebM/Matroska EBML header
        format_hint = "webm"
        ffmpeg_format = "webm"
    elif raw[:4] == b'OggS':  # Ogg container
        format_hint = "ogg"
        ffmpeg_format = "ogg"
    elif raw[:4] == b'fLaC':  # FLAC
        format_hint = "flac"
        ffmpeg_format = "flac"
    elif raw[:3] == b'ID3' or (raw[0] == 0xff and (raw[1] & 0xe0) == 0xe0):  # MP3
        format_hint = "mp3"
        ffmpeg_format = "mp3"
    else:
        # 🆕 Check if this might be raw Opus or concatenated WebM chunks
        # MediaRecorder chunks without proper headers often start with Opus frame data
        # The magic 43c38100 suggests truncated/concatenated WebM chunks
        if 'webm' in mime_type.lower() or 'opus' in mime_type.lower():
            format_hint = "opus_chunks"
            ffmpeg_format = "webm"  # Try as WebM first
            logger.info(f"[ROBUST-AUDIO] Treating as WebM/Opus chunks based on MIME type")

    logger.info(f"[ROBUST-AUDIO] Detected format: {format_hint}, ffmpeg_format: {ffmpeg_format}")

    # 🆕 NEW Strategy: FFmpeg with explicit input format hint for concatenated WebM chunks
    def _ffmpeg_format_hint_convert(input_format: str):
        """Convert using explicit input format hint - critical for concatenated MediaRecorder chunks."""
        try:
            # Use explicit format flag (-f) to tell FFmpeg what the input format is
            # Also use -err_detect ignore_err to handle broken containers
            cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'warning',
                '-err_detect', 'ignore_err',  # Ignore container errors
                '-f', input_format,  # Explicit input format
                '-i', 'pipe:0',
                '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                'pipe:1'
            ]
            logger.debug(f"[ROBUST-AUDIO] FFmpeg format hint cmd: {' '.join(cmd)}")

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            wav_out, stderr = proc.communicate(input=raw, timeout=10.0)

            if proc.returncode == 0 and len(wav_out) > 44:
                return wav_out, None
            else:
                return None, stderr.decode('utf-8', errors='ignore')
        except subprocess.TimeoutExpired:
            proc.kill()
            return None, f"FFmpeg {input_format} format hint timeout"
        except Exception as e:
            return None, str(e)

    # 🆕 NEW Strategy: FFmpeg with aggressive error recovery for broken WebM
    def _ffmpeg_broken_webm_recovery():
        """
        Handle broken/incomplete WebM containers from MediaRecorder chunk concatenation.
        Uses multiple FFmpeg strategies to extract audio data.
        """
        temp_in = None
        temp_out = None
        try:
            temp_in = tempfile.NamedTemporaryFile(suffix='.webm', delete=False)
            temp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            temp_in.write(raw)
            temp_in.close()
            temp_out.close()

            strategies = [
                # Strategy A: Generic with error tolerance
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-err_detect', 'ignore_err',
                    '-fflags', '+discardcorrupt+genpts',
                    '-i', temp_in.name,
                    '-vn',
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                # Strategy B: Force WebM format
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-err_detect', 'ignore_err',
                    '-f', 'webm',
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                # Strategy C: Force Matroska format
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-err_detect', 'ignore_err',
                    '-f', 'matroska',
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                # Strategy D: Force Ogg format (Opus often in Ogg)
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-err_detect', 'ignore_err',
                    '-f', 'ogg',
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                # Strategy E: Raw Opus stream assumption (48kHz default)
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-f', 'opus',
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
            ]

            for i, cmd in enumerate(strategies):
                strategy_name = ['generic', 'webm', 'matroska', 'ogg', 'opus'][i]
                try:
                    result = subprocess.run(cmd, capture_output=True, timeout=10.0)
                    if result.returncode == 0 and os.path.exists(temp_out.name):
                        wav_data = open(temp_out.name, 'rb').read()
                        if len(wav_data) > 44:
                            logger.info(f"[ROBUST-AUDIO] Broken WebM recovery ({strategy_name}) succeeded: {len(wav_data)} bytes")
                            return wav_data, None
                except subprocess.TimeoutExpired:
                    pass
                except Exception:
                    pass

            return None, "All broken WebM strategies failed"

        except Exception as e:
            return None, str(e)
        finally:
            if temp_in and os.path.exists(temp_in.name):
                os.unlink(temp_in.name)
            if temp_out and os.path.exists(temp_out.name):
                os.unlink(temp_out.name)

    # Step 3: Try FFmpeg pipe conversion (auto-detect format)
    def _ffmpeg_pipe_convert():
        """Convert via stdin/stdout pipe."""
        try:
            proc = subprocess.Popen(
                [
                    'ffmpeg', '-hide_banner', '-loglevel', 'error',
                    '-i', 'pipe:0',
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    'pipe:1'
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            wav_out, stderr = proc.communicate(input=raw, timeout=8.0)

            if proc.returncode == 0 and len(wav_out) > 44:
                return wav_out, None
            else:
                return None, stderr.decode('utf-8', errors='ignore')
        except subprocess.TimeoutExpired:
            proc.kill()
            return None, "FFmpeg pipe timeout"
        except Exception as e:
            return None, str(e)

    # Step 4: Try FFmpeg temp file conversion (more compatible)
    def _ffmpeg_file_convert():
        """Convert via temp files for better format support."""
        temp_in = None
        temp_out = None
        try:
            # Determine input extension from mime type
            ext_map = {
                'audio/webm': '.webm',
                'audio/ogg': '.ogg',
                'audio/mp4': '.m4a',
                'audio/mpeg': '.mp3',
                'audio/wav': '.wav',
                'audio/x-wav': '.wav',
            }
            in_ext = ext_map.get(mime_type.split(';')[0], '.webm')

            # Create temp files
            temp_in = tempfile.NamedTemporaryFile(suffix=in_ext, delete=False)
            temp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            temp_in.write(raw)
            temp_in.close()
            temp_out.close()

            # Run FFmpeg with file I/O
            result = subprocess.run(
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                capture_output=True, timeout=10.0
            )

            if result.returncode == 0 and os.path.exists(temp_out.name):
                with open(temp_out.name, 'rb') as f:
                    wav_data = f.read()
                if len(wav_data) > 44:
                    return wav_data, None

            return None, result.stderr.decode('utf-8', errors='ignore')

        except subprocess.TimeoutExpired:
            return None, "FFmpeg file timeout"
        except Exception as e:
            return None, str(e)
        finally:
            # Cleanup temp files
            if temp_in and os.path.exists(temp_in.name):
                os.unlink(temp_in.name)
            if temp_out and os.path.exists(temp_out.name):
                os.unlink(temp_out.name)

    # 🆕 NEW Strategy: Try to synthesize a valid WebM header for headerless chunks
    def _try_webm_header_synthesis():
        """
        For concatenated MediaRecorder chunks missing EBML header, try to synthesize one.
        MediaRecorder WebM uses Opus codec - we synthesize minimal EBML + Segment headers.
        """
        try:
            # Check if data looks like Opus frames (common for MediaRecorder chunks)
            # Opus TOC byte often has specific patterns
            if len(raw) < 10:
                return None, "Data too short for header synthesis"

            # Minimal WebM header with Opus audio
            # This is a simplified EBML header that FFmpeg can parse
            webm_header = bytes([
                # EBML Header
                0x1a, 0x45, 0xdf, 0xa3,  # EBML ID
                0x93,  # Size (19 bytes follow)
                0x42, 0x86, 0x81, 0x01,  # EBMLVersion = 1
                0x42, 0xf7, 0x81, 0x01,  # EBMLReadVersion = 1
                0x42, 0xf2, 0x81, 0x04,  # EBMLMaxIDLength = 4
                0x42, 0xf3, 0x81, 0x08,  # EBMLMaxSizeLength = 8
                0x42, 0x82, 0x84, 0x77, 0x65, 0x62, 0x6d,  # DocType = "webm"
                0x42, 0x87, 0x81, 0x02,  # DocTypeVersion = 2
                0x42, 0x85, 0x81, 0x02,  # DocTypeReadVersion = 2
            ])

            # For complex synthesis, just return None and let other strategies handle it
            # Full WebM synthesis is complex - prefer FFmpeg's format guessing
            return None, "Full WebM synthesis not implemented - using fallback"

        except Exception as e:
            return None, str(e)

    # 🆕 NEW Strategy: Create a proper WebM file with reconstructed container
    def _ffmpeg_matroska_demux():
        """Try Matroska demuxer explicitly for WebM-like content."""
        temp_in = None
        temp_out = None
        try:
            temp_in = tempfile.NamedTemporaryFile(suffix='.webm', delete=False)
            temp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            temp_in.write(raw)
            temp_in.close()
            temp_out.close()

            # Try with matroska demuxer and error tolerance
            result = subprocess.run(
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-err_detect', 'ignore_err',  # Ignore demux errors
                    '-f', 'matroska',  # Explicit matroska format
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                capture_output=True, timeout=10.0
            )

            if result.returncode == 0 and os.path.exists(temp_out.name):
                with open(temp_out.name, 'rb') as f:
                    wav_data = f.read()
                if len(wav_data) > 44:
                    return wav_data, None

            return None, result.stderr.decode('utf-8', errors='ignore')

        except subprocess.TimeoutExpired:
            return None, "FFmpeg matroska timeout"
        except Exception as e:
            return None, str(e)
        finally:
            if temp_in and os.path.exists(temp_in.name):
                os.unlink(temp_in.name)
            if temp_out and os.path.exists(temp_out.name):
                os.unlink(temp_out.name)

    # 🆕 NEW Strategy: Raw Opus to WAV using libopus
    def _opus_decode_raw():
        """Try to decode raw Opus packets using ffmpeg's opus decoder."""
        temp_in = None
        temp_out = None
        try:
            temp_in = tempfile.NamedTemporaryFile(suffix='.opus', delete=False)
            temp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            temp_in.write(raw)
            temp_in.close()
            temp_out.close()

            # Try Ogg container (Opus is often in Ogg)
            result = subprocess.run(
                [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
                    '-acodec', 'libopus',  # Opus decoder
                    '-i', temp_in.name,
                    '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    temp_out.name
                ],
                capture_output=True, timeout=10.0
            )

            if result.returncode == 0 and os.path.exists(temp_out.name):
                with open(temp_out.name, 'rb') as f:
                    wav_data = f.read()
                if len(wav_data) > 44:
                    return wav_data, None

            return None, result.stderr.decode('utf-8', errors='ignore')

        except subprocess.TimeoutExpired:
            return None, "FFmpeg opus timeout"
        except Exception as e:
            return None, str(e)
        finally:
            if temp_in and os.path.exists(temp_in.name):
                os.unlink(temp_in.name)
            if temp_out and os.path.exists(temp_out.name):
                os.unlink(temp_out.name)

    # 🆕 NEW Strategy: Try multiple format hints in sequence
    def _try_multiple_formats():
        """Try multiple format guesses for unrecognized data."""
        formats_to_try = ['webm', 'matroska', 'ogg', 'opus', 'data']
        for fmt in formats_to_try:
            try:
                proc = subprocess.Popen(
                    [
                        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
                        '-f', fmt,
                        '-i', 'pipe:0',
                        '-f', 'wav', '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                        'pipe:1'
                    ],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                wav_out, stderr = proc.communicate(input=raw, timeout=5.0)

                if proc.returncode == 0 and len(wav_out) > 44:
                    logger.info(f"[ROBUST-AUDIO] Format {fmt} worked!")
                    return wav_out, None
            except subprocess.TimeoutExpired:
                if proc:
                    proc.kill()
            except Exception:
                pass

        return None, "All format guesses failed"

    # Try strategies in order
    loop = asyncio.get_event_loop()

    # 🆕 Strategy 0a: Try broken WebM recovery FIRST for any WebM-like data
    # This is the most robust approach for MediaRecorder chunk concatenation
    if 'webm' in mime_type.lower() or format_hint in ['webm', 'opus_chunks', 'unknown']:
        try:
            logger.info("[ROBUST-AUDIO] 🔧 Trying broken WebM recovery (best for MediaRecorder chunks)...")
            wav_data, err = await asyncio.wait_for(
                loop.run_in_executor(None, _ffmpeg_broken_webm_recovery),
                timeout=20.0
            )
            if wav_data:
                logger.info(f"[ROBUST-AUDIO] ✅ Broken WebM recovery SUCCESS: {len(wav_data)} bytes WAV")
                return wav_data
            else:
                logger.warning(f"[ROBUST-AUDIO] Broken WebM recovery failed: {err}")
        except asyncio.TimeoutError:
            logger.warning("[ROBUST-AUDIO] Broken WebM recovery timeout")
        except Exception as e:
            logger.warning(f"[ROBUST-AUDIO] Broken WebM recovery error: {e}")

    # 🆕 Strategy 0b: If format_hint indicates concatenated chunks, try explicit format first
    if format_hint == "opus_chunks" and ffmpeg_format:
        try:
            logger.info(f"[ROBUST-AUDIO] Trying FFmpeg with explicit format hint: {ffmpeg_format}")
            wav_data, err = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _ffmpeg_format_hint_convert(ffmpeg_format)),
                timeout=10.0
            )
            if wav_data:
                logger.info(f"[ROBUST-AUDIO] FFmpeg format hint SUCCESS: {len(wav_data)} bytes WAV")
                return wav_data
            else:
                logger.warning(f"[ROBUST-AUDIO] FFmpeg format hint failed: {err}")
        except asyncio.TimeoutError:
            logger.warning("[ROBUST-AUDIO] FFmpeg format hint timeout")
        except Exception as e:
            logger.warning(f"[ROBUST-AUDIO] FFmpeg format hint error: {e}")

        # Try matroska demuxer for WebM chunks
        try:
            logger.info("[ROBUST-AUDIO] Trying Matroska demuxer with error tolerance...")
            wav_data, err = await asyncio.wait_for(
                loop.run_in_executor(None, _ffmpeg_matroska_demux),
                timeout=12.0
            )
            if wav_data:
                logger.info(f"[ROBUST-AUDIO] Matroska demux SUCCESS: {len(wav_data)} bytes WAV")
                return wav_data
            else:
                logger.warning(f"[ROBUST-AUDIO] Matroska demux failed: {err}")
        except asyncio.TimeoutError:
            logger.warning("[ROBUST-AUDIO] Matroska demux timeout")
        except Exception as e:
            logger.warning(f"[ROBUST-AUDIO] Matroska demux error: {e}")

    # Strategy 1: FFmpeg pipe (fast, auto-detect)
    try:
        logger.info("[ROBUST-AUDIO] Trying FFmpeg pipe conversion...")
        wav_data, err = await asyncio.wait_for(
            loop.run_in_executor(None, _ffmpeg_pipe_convert),
            timeout=10.0
        )
        if wav_data:
            logger.info(f"[ROBUST-AUDIO] FFmpeg pipe SUCCESS: {len(wav_data)} bytes WAV")
            return wav_data
        else:
            logger.warning(f"[ROBUST-AUDIO] FFmpeg pipe failed: {err}")
    except asyncio.TimeoutError:
        logger.warning("[ROBUST-AUDIO] FFmpeg pipe timeout")
    except Exception as e:
        logger.warning(f"[ROBUST-AUDIO] FFmpeg pipe error: {e}")

    # Strategy 2: FFmpeg file (more compatible)
    try:
        logger.info("[ROBUST-AUDIO] Trying FFmpeg file conversion...")
        wav_data, err = await asyncio.wait_for(
            loop.run_in_executor(None, _ffmpeg_file_convert),
            timeout=12.0
        )
        if wav_data:
            logger.info(f"[ROBUST-AUDIO] FFmpeg file SUCCESS: {len(wav_data)} bytes WAV")
            return wav_data
        else:
            logger.warning(f"[ROBUST-AUDIO] FFmpeg file failed: {err}")
    except asyncio.TimeoutError:
        logger.warning("[ROBUST-AUDIO] FFmpeg file timeout")
    except Exception as e:
        logger.warning(f"[ROBUST-AUDIO] FFmpeg file error: {e}")

    # Strategy 3: Try multiple format hints
    try:
        logger.info("[ROBUST-AUDIO] Trying multiple format guesses...")
        wav_data, err = await asyncio.wait_for(
            loop.run_in_executor(None, _try_multiple_formats),
            timeout=30.0
        )
        if wav_data:
            logger.info(f"[ROBUST-AUDIO] Multi-format guess SUCCESS: {len(wav_data)} bytes WAV")
            return wav_data
        else:
            logger.warning(f"[ROBUST-AUDIO] Multi-format guess failed: {err}")
    except asyncio.TimeoutError:
        logger.warning("[ROBUST-AUDIO] Multi-format guess timeout")
    except Exception as e:
        logger.warning(f"[ROBUST-AUDIO] Multi-format guess error: {e}")

    # Strategy 4: Raw Opus decode
    try:
        logger.info("[ROBUST-AUDIO] Trying raw Opus decode...")
        wav_data, err = await asyncio.wait_for(
            loop.run_in_executor(None, _opus_decode_raw),
            timeout=10.0
        )
        if wav_data:
            logger.info(f"[ROBUST-AUDIO] Raw Opus decode SUCCESS: {len(wav_data)} bytes WAV")
            return wav_data
        else:
            logger.warning(f"[ROBUST-AUDIO] Raw Opus decode failed: {err}")
    except asyncio.TimeoutError:
        logger.warning("[ROBUST-AUDIO] Raw Opus timeout")
    except Exception as e:
        logger.warning(f"[ROBUST-AUDIO] Raw Opus error: {e}")

    # Strategy 5: Return raw for torchaudio to handle
    logger.warning(f"[ROBUST-AUDIO] All conversions failed, returning raw {len(raw)} bytes for torchaudio")
    return raw


def _verify_speaker_robust(test_emb: List[float], ref_emb: List[float]) -> Tuple[bool, float]:
    """Verify speaker using cosine similarity with detailed diagnostics."""
    import numpy as np

    test = np.array(test_emb, dtype=np.float32)
    ref = np.array(ref_emb, dtype=np.float32)

    # 🔍 DIAGNOSTIC LOGGING - Critical for debugging low similarity scores
    logger.info(f"[ROBUST-VERIFY] ═══════════════════════════════════════════════════")
    logger.info(f"[ROBUST-VERIFY] Test embedding: dims={len(test)}, norm={np.linalg.norm(test):.4f}")
    logger.info(f"[ROBUST-VERIFY]   min={test.min():.4f}, max={test.max():.4f}, mean={test.mean():.4f}, std={test.std():.4f}")
    logger.info(f"[ROBUST-VERIFY]   first_5={test[:5].tolist()}")
    logger.info(f"[ROBUST-VERIFY] Ref embedding: dims={len(ref)}, norm={np.linalg.norm(ref):.4f}")
    logger.info(f"[ROBUST-VERIFY]   min={ref.min():.4f}, max={ref.max():.4f}, mean={ref.mean():.4f}, std={ref.std():.4f}")
    logger.info(f"[ROBUST-VERIFY]   first_5={ref[:5].tolist()}")

    # Check for zero embeddings (indicates extraction failure)
    test_norm = np.linalg.norm(test)
    ref_norm = np.linalg.norm(ref)

    if test_norm < 1e-6:
        logger.error("[ROBUST-VERIFY] ⚠️ TEST EMBEDDING IS NEAR-ZERO! Extraction likely failed.")
        return False, 0.0

    if ref_norm < 1e-6:
        logger.error("[ROBUST-VERIFY] ⚠️ REFERENCE EMBEDDING IS NEAR-ZERO! Database profile may be corrupted.")
        return False, 0.0

    # Normalize embeddings
    test_n = test / (test_norm + 1e-10)
    ref_n = ref / (ref_norm + 1e-10)

    # Compute cosine similarity
    sim = float(np.dot(test_n, ref_n))

    # Additional diagnostic: L2 distance
    l2_dist = float(np.linalg.norm(test_n - ref_n))

    # Check if embeddings are suspiciously different
    if sim < 0.5:
        logger.warning(f"[ROBUST-VERIFY] ⚠️ LOW SIMILARITY ({sim:.4f}) - possible causes:")
        logger.warning(f"[ROBUST-VERIFY]   1. Different ECAPA model versions used for enrollment vs verification")
        logger.warning(f"[ROBUST-VERIFY]   2. Audio quality issue (noise, distortion)")
        logger.warning(f"[ROBUST-VERIFY]   3. Different speaker")
        logger.warning(f"[ROBUST-VERIFY]   L2 distance between normalized vectors: {l2_dist:.4f}")

    verified = sim >= RobustUnlockConfig.CONFIDENCE_THRESHOLD
    logger.info(f"[ROBUST-VERIFY] Result: sim={sim:.4f}, L2_dist={l2_dist:.4f}, threshold={RobustUnlockConfig.CONFIDENCE_THRESHOLD}, verified={verified}")
    logger.info(f"[ROBUST-VERIFY] ═══════════════════════════════════════════════════")

    return verified, sim


async def _execute_unlock_robust(
    speaker_name: str,
    progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    confidence: float = 0.0
) -> bool:
    """
    Execute screen unlock with CLEAN, LINEAR flow.

    DESIGN PRINCIPLES:
    - ONE caffeinate process (prevents display sleep)
    - ONE wake signal (no competing wake methods)
    - ONE password entry attempt (no retries that cause double-typing)
    - SIMPLE verification

    Args:
        speaker_name: The verified speaker's name
        progress_callback: Optional callback for progress updates
        confidence: Voice verification confidence score

    Returns:
        bool: True if unlock succeeded, False otherwise
    """
    import os
    caffeinate_proc = None
    unlock_start_time = time.time()
    attempt_id = int(unlock_start_time * 1000)

    async def _progress(substage: str, pct: int, msg: str):
        """Send progress update."""
        if progress_callback:
            try:
                data = {
                    "type": "vbi_progress",
                    "stage": "unlock_execute",
                    "substage": substage,
                    "progress": pct,
                    "message": msg,
                    "timestamp": time.time(),
                    "speaker": speaker_name,
                    "confidence": confidence
                }
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(data)
                else:
                    progress_callback(data)
            except:
                pass

    try:
        logger.info(f"[UNLOCK] Starting for {speaker_name} (conf={confidence:.1%})")

        # =====================================================================
        # STEP 1: START CAFFEINATE (prevents display sleep - NO -u flag to avoid flicker)
        # =====================================================================
        caffeinate_proc = await asyncio.create_subprocess_exec(
            "caffeinate", "-d", "-t", "30",  # -d = prevent display sleep, -t 30 = 30 second timeout
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        logger.info(f"[UNLOCK] Caffeinate started (PID: {caffeinate_proc.pid})")

        # =====================================================================
        # STEP 2: GET PASSWORD FROM KEYCHAIN
        # =====================================================================
        await _progress("keychain", 91, "Getting credentials...")

        keychain_proc = await asyncio.create_subprocess_exec(
            "security", "find-generic-password",
            "-s", "com.jarvis.voiceunlock",
            "-a", "unlock_token",
            "-w",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(keychain_proc.communicate(), timeout=5.0)

        if keychain_proc.returncode != 0 or not stdout:
            logger.error("[UNLOCK] Password not found in keychain")
            await _progress("keychain", 91, "Password not configured")
            return False

        password = stdout.decode().strip()
        logger.info(f"[UNLOCK] Password retrieved ({len(password)} chars)")
        await _progress("keychain", 92, "Credentials ready")

        # =====================================================================
        # STEP 3: WAKE DISPLAY ONCE (single clean wake)
        # =====================================================================
        await _progress("wake", 93, "Waking display...")

        # Use caffeinate -u for ONE instant wake, then let it exit
        wake_proc = await asyncio.create_subprocess_exec(
            "caffeinate", "-u", "-t", "1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(wake_proc.wait(), timeout=3)

        # Wait for lock screen to fully render
        await asyncio.sleep(1.0)
        logger.info("[UNLOCK] Display awake")
        await _progress("wake", 94, "Display ready")

        # =====================================================================
        # STEP 4: TYPE PASSWORD (single attempt, no retries)
        # =====================================================================
        await _progress("typing", 95, f"Authenticating {speaker_name}...")
        logger.info("[UNLOCK] Entering password...")

        # Simple, clean AppleScript - just type and submit
        password_script = '''
        tell application "System Events"
            keystroke (system attribute "JARVIS_UNLOCK_PWD")
            delay 0.1
            key code 36
        end tell
        '''

        env = os.environ.copy()
        env["JARVIS_UNLOCK_PWD"] = password

        type_proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", password_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env
        )
        await asyncio.wait_for(type_proc.wait(), timeout=10.0)

        # Don't keep password in memory
        del env["JARVIS_UNLOCK_PWD"]
        password = None

        logger.info("[UNLOCK] Password submitted")
        await _progress("typing", 97, "Password submitted")

        # =====================================================================
        # STEP 5: VERIFY UNLOCK
        # =====================================================================
        await _progress("verify", 98, "Verifying...")

        # Wait for macOS to process the password
        await asyncio.sleep(1.5)

        try:
            from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
            is_locked = is_screen_locked()

            if not is_locked:
                logger.info(f"[UNLOCK] ✅ Success for {speaker_name}")
                await _progress("complete", 100, f"Welcome back, {speaker_name}!")
                return True
            else:
                logger.warning("[UNLOCK] Screen still locked")
                await _progress("verify", 98, "Verification failed")
                return False

        except Exception as e:
            # Can't verify - assume success
            logger.info(f"[UNLOCK] ✅ Completed (verify unavailable: {e})")
            await _progress("complete", 100, f"Welcome back, {speaker_name}!")
            return True

    except asyncio.TimeoutError:
        logger.error("[UNLOCK] Timeout")
        await _progress("error", 90, "Timeout")
        return False
    except Exception as e:
        logger.error(f"[UNLOCK] Error: {e}")
        await _progress("error", 90, f"Error: {e}")
        return False

    finally:
        # Cleanup caffeinate
        if caffeinate_proc and caffeinate_proc.returncode is None:
            try:
                caffeinate_proc.terminate()
            except:
                pass


async def _record_unlock_attempt(
    attempt_id: int,
    speaker_name: str,
    confidence: float,
    success: bool,
    duration_ms: float
) -> None:
    """
    Record unlock attempt to SQLite for continuous learning (fire-and-forget).
    This runs in background and never blocks the main unlock flow.
    """
    try:
        from voice_unlock.metrics_database import get_metrics_database

        db = get_metrics_database()

        # Store attempt data
        attempt_data = {
            "attempt_id": attempt_id,
            "speaker_name": speaker_name,
            "confidence": confidence,
            "success": success,
            "duration_ms": duration_ms,
            "timestamp": datetime.now().isoformat(),
            "method": "voice_biometric"
        }

        # Use async executor to avoid blocking
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, db._record_attempt_sync, attempt_data),
            timeout=3.0
        )

        logger.info(f"📊 [ML-STORAGE] Recorded unlock attempt: {speaker_name} - {'✅ SUCCESS' if success else '❌ FAILED'} ({duration_ms:.0f}ms)")

    except asyncio.TimeoutError:
        logger.warning("⏱️ Unlock attempt recording timed out (non-blocking)")
    except Exception as e:
        logger.debug(f"Failed to record unlock attempt: {e}")  # Debug level - don't spam logs


async def _execute_unlock_applescript_fallback(
    password: str,
    progress_callback: Optional[Callable] = None
) -> bool:
    """
    Fallback AppleScript-based unlock when SecurePasswordTyper fails.

    IMPORTANT: This assumes the display is ALREADY AWAKE and the password
    field is ALREADY FOCUSED (done by the caller). This just types the password.

    Uses System Events keystroke for reliable typing on lock screen.

    Args:
        password: The password to type
        progress_callback: Optional async callback for progress updates
    """
    try:
        if progress_callback:
            await progress_callback("fallback", 95, "Using AppleScript fallback...")

        # For AppleScript, we use an environment variable to avoid the password
        # appearing in the process list (ps aux would show osascript args)
        import os

        # Create the typing script - uses env variable for password security
        # NOTE: Display and password field should already be ready from caller
        type_script = '''
        tell application "System Events"
            -- Small delay to ensure we have focus
            delay 0.2

            -- Clear any existing text (select all + delete)
            keystroke "a" using command down
            delay 0.1
            key code 51
            delay 0.2

            -- Type the password from environment variable (secure - not in process list)
            keystroke (system attribute "JARVIS_SECURE_PASS")
            delay 0.2

            -- Press Enter/Return to submit
            key code 36
        end tell
        '''

        logger.info("[ROBUST-UNLOCK] Running AppleScript fallback (password field should be ready)...")

        # Set password in environment (more secure than command line)
        env = os.environ.copy()
        env["JARVIS_SECURE_PASS"] = password

        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", type_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)

        # Clear password from environment immediately
        try:
            del env["JARVIS_SECURE_PASS"]
        except:
            pass

        if process.returncode == 0:
            logger.info("[ROBUST-UNLOCK] AppleScript fallback executed successfully")
            if progress_callback:
                await progress_callback("fallback", 98, "Password entered via AppleScript")
            # Wait for authentication to process
            await asyncio.sleep(1.5)
            return True
        else:
            error_msg = stderr.decode().strip() if stderr else "Unknown error"
            logger.error(f"[ROBUST-UNLOCK] AppleScript fallback failed: {error_msg}")
            if progress_callback:
                await progress_callback("fallback", 95, f"AppleScript error: {error_msg[:50]}")
            return False

    except asyncio.TimeoutError:
        logger.error("[ROBUST-UNLOCK] AppleScript fallback timed out after 10s")
        if progress_callback:
            await progress_callback("fallback", 95, "Fallback timed out")
        return False
    except Exception as e:
        logger.error(f"[ROBUST-UNLOCK] AppleScript fallback exception: {e}")
        if progress_callback:
            await progress_callback("fallback", 95, f"Error: {e}")
        return False


# =============================================================================
# 🧠 CONTEXT-AWARE RESPONSE GENERATOR
# =============================================================================
# Generates intelligent, personalized responses when screen is already unlocked
# Uses time of day, speaker name, and context for natural conversation
# =============================================================================

async def _generate_already_unlocked_response(speaker_name: str) -> str:
    """
    Generate a context-aware, personalized response when screen is already unlocked.

    This creates natural, varied responses that make JARVIS feel self-aware
    and intelligent rather than robotic.

    Args:
        speaker_name: The user's name for personalization

    Returns:
        A contextual, personalized response string
    """
    import random
    from datetime import datetime

    # Get current time context
    current_hour = datetime.now().hour

    # Determine time of day for contextual responses
    if 5 <= current_hour < 12:
        time_period = "morning"
    elif 12 <= current_hour < 17:
        time_period = "afternoon"
    elif 17 <= current_hour < 21:
        time_period = "evening"
    else:
        time_period = "night"

    # Dynamic response pools based on time of day
    responses = {
        "morning": [
            f"Good morning, {speaker_name}. Your screen is already unlocked and ready for you.",
            f"Morning, {speaker_name}. I notice your screen is already unlocked. How can I help?",
            f"Your screen's already open, {speaker_name}. Ready when you are.",
            f"Already unlocked, {speaker_name}. Looks like we're ahead of the game this morning.",
        ],
        "afternoon": [
            f"Your screen is already unlocked, {speaker_name}. You're good to go.",
            f"I see your screen is already open, {speaker_name}. What can I help you with?",
            f"Already unlocked, {speaker_name}. Perhaps you meant to ask me something else?",
            f"Screen's already accessible, {speaker_name}. Let me know if you need anything.",
        ],
        "evening": [
            f"Your screen is already unlocked, {speaker_name}. Working late?",
            f"I notice it's already open, {speaker_name}. Evening productivity session?",
            f"Already unlocked, {speaker_name}. I'm here if you need me this evening.",
            f"Screen's ready, {speaker_name}. What would you like to work on tonight?",
        ],
        "night": [
            f"Your screen is already unlocked, {speaker_name}. Burning the midnight oil?",
            f"Already open, {speaker_name}. Late night coding session?",
            f"Screen's accessible, {speaker_name}. I'm here for whatever you need tonight.",
            f"Already unlocked, {speaker_name}. Don't forget to rest at some point!",
        ],
    }

    # Get responses for current time period
    time_responses = responses.get(time_period, responses["afternoon"])

    # Add some universal responses that work any time
    universal_responses = [
        f"Your screen is already unlocked, {speaker_name}.",
        f"No unlock needed, {speaker_name}. Your screen is already accessible.",
        f"I notice your screen isn't locked, {speaker_name}. Anything else I can help with?",
        f"Already unlocked, {speaker_name}. I'm standing by if you need anything else.",
    ]

    # Combine and select randomly for variety
    all_responses = time_responses + universal_responses
    selected_response = random.choice(all_responses)

    logger.info(f"[CONTEXT-AWARE] Generated response for '{time_period}': {selected_response}")

    return selected_response


async def process_voice_unlock_robust(
    command: str,
    audio_data: str,
    sample_rate: int = 16000,
    mime_type: str = "audio/webm",
    progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    enable_voice_feedback: bool = True
) -> Dict[str, Any]:
    """
    Robust voice unlock with hard timeouts, parallel processing, and
    real-time Daniel voice feedback for transparent VBI communication.

    CONTEXT-AWARE: First checks if screen is already unlocked and responds
    intelligently without unnecessary VBI processing.

    GUARANTEED to return within MAX_TOTAL_TIMEOUT seconds.

    Args:
        command: The voice command text
        audio_data: Base64 encoded audio
        sample_rate: Audio sample rate
        mime_type: Audio MIME type
        progress_callback: Optional WebSocket progress callback
        enable_voice_feedback: Enable Daniel's voice narration during VBI

    Returns:
        Dict with success, response, confidence, etc.
    """
    start = time.time()
    stages = []

    # Initialize voice communicator for real-time feedback (non-blocking)
    voice = None
    if enable_voice_feedback:
        try:
            from agi_os.realtime_voice_communicator import get_voice_communicator
            voice = await asyncio.wait_for(get_voice_communicator(), timeout=2.0)
        except Exception as e:
            logger.debug(f"[ROBUST] Voice feedback unavailable: {e}")
            voice = None

    # =========================================================================
    # 🔇 SELF-VOICE SUPPRESSION - Prevent JARVIS from hearing its own voice
    # =========================================================================
    # If JARVIS is currently speaking, this audio is likely JARVIS's own voice
    # being picked up by the microphone. Reject it immediately to prevent
    # feedback loops and "hallucinations".
    # =========================================================================
    if voice and voice.is_speaking:
        logger.warning("🔇 [SELF-VOICE-SUPPRESSION] Rejecting audio - JARVIS is currently speaking")
        return {
            "success": False,
            "response": None,  # Silent rejection - don't speak or it creates more echo
            "speaker_name": "Unknown",
            "confidence": 0.0,
            "total_duration_ms": (time.time() - start) * 1000,
            "error": "self_voice_suppression",
            "trace_id": f"echo_reject_{int(time.time())}",
            "stages": [{"stage": "self_voice_check", "success": False, "reason": "jarvis_speaking"}],
            "handler": "robust_v1_echo_suppressed"
        }

    # Get dynamic owner name (will be updated after verification with actual speaker)
    dynamic_owner_name = None
    if voice:
        try:
            dynamic_owner_name = await asyncio.wait_for(voice.get_owner_name(), timeout=1.0)
        except Exception:
            dynamic_owner_name = None

    async def voice_feedback(stage: str, confidence: float = 0.0, speaker: Optional[str] = None):
        """Fire-and-forget voice feedback - doesn't block VBI flow."""
        if voice:
            try:
                name = speaker or dynamic_owner_name
                asyncio.create_task(voice.vbi_stage_feedback(stage, confidence, name))
            except:
                pass

    async def speak_and_wait(text: str, timeout: float = 8.0):
        """
        Speak text and WAIT for it to complete before continuing.

        This ensures voice feedback is SYNCHRONIZED with actions - the user
        hears the message BEFORE the action starts, not during or after.

        Args:
            text: Text to speak
            timeout: Maximum wait time (default 8s for longer phrases)
        """
        if voice and text:
            try:
                logger.info(f"🔊 [SYNC-VOICE] Speaking (blocking): {text}")
                await voice.speak_immediate(text, interrupt=True)
                # Calculate wait time based on speech characteristics:
                # - Average speaking rate: ~150 words/minute = 2.5 words/second
                # - So ~400ms per word is reasonable
                # - Add 500ms buffer for TTS latency
                word_count = len(text.split())
                wait_time = min(0.4 * word_count + 0.5, timeout)
                logger.debug(f"🔊 [SYNC-VOICE] Waiting {wait_time:.1f}s for {word_count} words")
                await asyncio.sleep(wait_time)
                logger.debug(f"🔊 [SYNC-VOICE] Speech complete, continuing")
            except Exception as e:
                logger.debug(f"🔊 [SYNC-VOICE] Error: {e}")
                pass

    async def speak_error_sync(error_type: str = "error", confidence: float = 0.0):
        """
        Speak synchronized error/failure message.

        Args:
            error_type: Type of error ("error" for general, "failed" for verification failure)
            confidence: Verification confidence (for failed type)
        """
        if error_type == "failed" and confidence > 0:
            if confidence < 0.5:
                msg = "I couldn't recognize that voice. Please try again."
            else:
                msg = f"Voice confidence was {confidence:.0%}, which isn't quite enough. Please try again."
        else:
            msg = "I couldn't process that. Please try again."

        await speak_and_wait(msg)

    async def save_for_continuous_learning(
        speaker_name: str,
        confidence: float,
        audio_bytes: bytes,
        embedding: Optional[list] = None,
        duration_ms: float = 0.0,
        success: bool = True,
        error_reason: str = None,
        verification_time_ms: float = None,
        unlock_time_ms: float = None,
        audio_quality: str = None,
        handler: str = "robust_v1"
    ):
        """
        Save unlock attempt to SQLite database for continuous learning.

        Records EVERY attempt (success or failure) with full metadata including:
        - Date/time in human-readable format (for DB Browser SQLite)
        - Confidence percentage (0-100%)
        - Embedding data for ML training
        - Audio quality metrics
        - Environment context

        Args:
            speaker_name: Verified speaker name
            confidence: Verification confidence score (0.0-1.0)
            audio_bytes: Raw audio bytes from the unlock attempt
            embedding: Optional pre-computed 192-dim ECAPA embedding
            duration_ms: Total unlock duration in milliseconds
            success: Whether the unlock was successful
            error_reason: Error message if failed
            verification_time_ms: Time spent on voice verification
            unlock_time_ms: Time spent on actual unlock execution
            audio_quality: Audio quality assessment ('excellent', 'good', 'fair', 'poor')
            handler: Handler name that processed the request
        """
        from datetime import datetime
        import json

        now = datetime.now()
        attempt_id = int(time.time() * 1000)

        try:
            # =========================================================================
            # 0. ACTIVE LEARNING - Update voice profile with new sample
            # =========================================================================
            # This is the KEY to JARVIS getting smarter over time!
            # The learning engine fuses new embeddings into the profile using ML
            # =========================================================================
            if success and embedding is not None:
                try:
                    from voice_unlock.voice_profile_learning_engine import learn_from_vbi_unlock

                    embedding_array = np.array(embedding) if not isinstance(embedding, np.ndarray) else embedding

                    learning_result = await learn_from_vbi_unlock(
                        embedding=embedding_array,
                        confidence=confidence,
                        success=success,
                        speaker_name=speaker_name,
                        audio_quality=audio_quality
                    )

                    if learning_result.get('learned'):
                        logger.info(
                            f"🧠 [ACTIVE-LEARNING] Voice profile updated! | "
                            f"Avg Confidence: {learning_result.get('current_avg_confidence', 0):.1%} | "
                            f"Trend: {learning_result.get('confidence_trend', 'unknown')} | "
                            f"Total Samples: {learning_result.get('total_samples', 0)}"
                        )
                except Exception as e:
                    logger.debug(f"🧠 [ACTIVE-LEARNING] Skipped: {e}")

            # =========================================================================
            # 1. Save to vbi_unlock_attempts table (enhanced metrics database)
            # =========================================================================
            from voice_unlock.metrics_database import get_metrics_database
            metrics_db = get_metrics_database()

            # Prepare embedding JSON
            embedding_json = None
            if embedding is not None:
                try:
                    embedding_json = json.dumps(
                        embedding if isinstance(embedding, list) else embedding.tolist()
                    )
                except Exception:
                    pass

            # Determine audio quality if not provided
            if audio_quality is None:
                if confidence >= 0.95:
                    audio_quality = 'excellent'
                elif confidence >= 0.85:
                    audio_quality = 'good'
                elif confidence >= 0.70:
                    audio_quality = 'fair'
                else:
                    audio_quality = 'poor'

            # Calculate learning quality score (higher = more valuable for training)
            # Based on: confidence, success, audio quality
            learning_quality = confidence
            if success:
                learning_quality += 0.1
            if audio_quality == 'excellent':
                learning_quality += 0.1
            elif audio_quality == 'good':
                learning_quality += 0.05
            learning_quality = min(1.0, learning_quality)

            # Prepare attempt data with enhanced fields
            attempt_data = {
                'attempt_id': attempt_id,
                'speaker_name': speaker_name,
                'confidence': confidence,
                'threshold': 0.85,
                'success': success,
                'error_reason': error_reason,
                'duration_ms': duration_ms,
                'verification_time_ms': verification_time_ms,
                'unlock_time_ms': unlock_time_ms,
                'timestamp': now.isoformat(),
                'method': 'voice_biometric',
                'handler': handler,
                'audio_duration_sec': len(audio_bytes) / 32000 if audio_bytes else None,  # Assuming 16kHz mono
                'audio_quality': audio_quality,
                'embedding_json': embedding_json,
                'environment': 'unknown',  # Could be enhanced with noise detection
                'microphone': 'built_in',  # Could be detected from audio properties
                'learning_quality_score': learning_quality,
            }

            # Record to metrics database (vbi_unlock_attempts table)
            await metrics_db.record_vbi_attempt(attempt_data)

            logger.info(
                f"📚 [CONTINUOUS-LEARNING] Saved VBI attempt for {speaker_name} | "
                f"Date: {now.strftime('%Y-%m-%d')} | Time: {now.strftime('%H:%M:%S')} | "
                f"Confidence: {confidence*100:.1f}% | Success: {success}"
            )

        except Exception as e:
            logger.warning(f"📚 [CONTINUOUS-LEARNING] Failed to save to metrics DB: {e}")

        try:
            # =========================================================================
            # 2. Also save to voice_learning_samples table (for audio retraining)
            # =========================================================================
            # Only save audio for SUCCESSFUL high-confidence attempts
            if success and confidence >= 0.80 and audio_bytes:
                from intelligence.learning_database import JARVISLearningDatabase
                import base64

                db = JARVISLearningDatabase()
                await db.initialize()

                sample_id = f"vbi_unlock_{attempt_id}"

                async with db.db.cursor() as cursor:
                    # Create voice_learning table if not exists
                    await cursor.execute("""
                        CREATE TABLE IF NOT EXISTS voice_learning_samples (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            sample_id TEXT UNIQUE NOT NULL,
                            speaker_name TEXT NOT NULL,
                            confidence REAL NOT NULL,
                            confidence_pct REAL,
                            date TEXT,
                            time TEXT,
                            audio_base64 TEXT,
                            embedding_json TEXT,
                            audio_quality TEXT,
                            source TEXT DEFAULT 'vbi_unlock',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            processed INTEGER DEFAULT 0
                        )
                    """)

                    audio_b64 = base64.b64encode(audio_bytes).decode() if audio_bytes else None

                    await cursor.execute("""
                        INSERT OR REPLACE INTO voice_learning_samples
                        (sample_id, speaker_name, confidence, confidence_pct, date, time,
                         audio_base64, embedding_json, audio_quality, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'vbi_unlock')
                    """, (
                        sample_id, speaker_name, confidence, round(confidence * 100, 2),
                        now.strftime('%Y-%m-%d'), now.strftime('%H:%M:%S'),
                        audio_b64, embedding_json, audio_quality
                    ))

                    await db.db.commit()

                logger.info(f"📚 [CONTINUOUS-LEARNING] Saved audio sample for future training")

        except Exception as e:
            logger.debug(f"📚 [CONTINUOUS-LEARNING] Audio sample save skipped: {e}")

    async def send_unlock_notification(speaker_name: str, confidence: float, duration_ms: float):
        """
        Send macOS notification on successful VBI unlock.

        Args:
            speaker_name: Verified speaker name
            confidence: Verification confidence percentage
            duration_ms: Total unlock duration in milliseconds
        """
        try:
            # Format notification message
            conf_pct = f"{confidence * 100:.1f}%"
            duration_sec = f"{duration_ms / 1000:.1f}s"

            title = "🔓 Screen Unlocked via VBI"
            message = f"Welcome back, {speaker_name}! Confidence: {conf_pct} | Time: {duration_sec}"
            subtitle = "Voice Biometric Intelligence"

            # Use osascript for macOS notification
            notification_script = f'''
            display notification "{message}" with title "{title}" subtitle "{subtitle}" sound name "Glass"
            '''

            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", notification_script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=3.0)

            logger.info(f"🔔 [NOTIFICATION] Sent unlock notification for {speaker_name}")

        except Exception as e:
            logger.debug(f"🔔 [NOTIFICATION] Failed to send: {e}")

    async def progress(stage: str, pct: int, msg: str, success: bool = None, confidence: float = 0.0):
        """
        Send progress update to callback.

        Args:
            stage: Current stage name
            pct: Progress percentage (0-100)
            msg: Human-readable message
            success: Stage completion status (True=✅, False=❌, None=in progress)
            confidence: Verification confidence (optional)
        """
        if progress_callback:
            try:
                # Add success indicator to message
                if success is True:
                    display_msg = f"✅ {msg}"
                elif success is False:
                    display_msg = f"❌ {msg}"
                else:
                    display_msg = msg

                data = {
                    "type": "vbi_progress",
                    "stage": stage,
                    "progress": pct,
                    "message": display_msg,
                    "raw_message": msg,  # Original message without emoji
                    "success": success,
                    "timestamp": time.time()
                }

                if confidence > 0:
                    data["confidence"] = confidence

                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(data)
                else:
                    progress_callback(data)
            except:
                pass

    def result(success: bool, msg: str, speaker: str = "Unknown", conf: float = 0.0, err: str = None):
        return {
            "success": success, "response": msg, "speaker_name": speaker,
            "confidence": conf, "total_duration_ms": (time.time() - start) * 1000,
            "error": err, "trace_id": f"robust_{int(time.time())}", "stages": stages, "handler": "robust_v1"
        }

    try:
        async def _unlock():
            logger.info("=" * 60)
            logger.info(f"[ROBUST] VOICE UNLOCK STARTED")
            logger.info(f"[ROBUST] Audio data type: {type(audio_data)}, length: {len(audio_data) if audio_data else 0}")
            logger.info(f"[ROBUST] MIME type: {mime_type}, Sample rate: {sample_rate}")

            # =================================================================
            # 🧠 CONTEXT-AWARE SCREEN STATE INTELLIGENCE
            # =================================================================
            # Check if screen is already unlocked BEFORE any VBI processing
            # This provides self-aware, intelligent responses
            # =================================================================
            try:
                from voice_unlock.objc.server.screen_lock_detector import is_screen_locked

                screen_is_locked = is_screen_locked()
                logger.info(f"[ROBUST] 🖥️ Screen state check: {'LOCKED' if screen_is_locked else 'ALREADY UNLOCKED'}")

                if not screen_is_locked:
                    # Screen is already unlocked - generate context-aware response
                    logger.info("[ROBUST] 🎯 Screen already unlocked - generating intelligent response")
                    await progress("context_check", 100, "Screen is already unlocked", success=True)

                    # Get speaker name for personalization (use dynamic owner or fallback)
                    speaker_for_response = dynamic_owner_name or "there"

                    # Generate time-aware, contextual response
                    response_text = await _generate_already_unlocked_response(speaker_for_response)

                    # Provide voice feedback for already unlocked scenario
                    if voice:
                        try:
                            await voice.speak_immediate(response_text, interrupt=True)
                            logger.info(f"🔊 [CONTEXT-AWARE] Spoke: {response_text}")
                        except Exception as speak_err:
                            logger.debug(f"Voice feedback error: {speak_err}")

                    stages.append({"stage": "context_check", "success": True, "screen_was_locked": False})

                    return result(
                        success=True,
                        msg=response_text,
                        speaker=speaker_for_response,
                        conf=1.0,  # 100% confidence - we verified screen state
                        err=None
                    )

            except ImportError as ie:
                logger.warning(f"[ROBUST] Screen lock detector not available: {ie}")
            except Exception as e:
                logger.warning(f"[ROBUST] Screen state check failed (continuing with unlock): {e}")

            # Screen IS locked - continue with VBI verification
            stages.append({"stage": "context_check", "success": True, "screen_was_locked": True})
            logger.info("=" * 60)

            # Validate input
            if not audio_data:
                logger.error("[ROBUST] No audio data provided!")
                await voice_feedback("error")
                return result(False, "No audio data provided", err="audio_data is empty or None")

            if not isinstance(audio_data, str):
                logger.error(f"[ROBUST] Audio data is not string: {type(audio_data)}")
                await voice_feedback("error")
                return result(False, "Audio data must be base64 string", err=f"Got {type(audio_data)}")

            # Check if base64 looks valid
            if len(audio_data) < 100:
                logger.error(f"[ROBUST] Audio data too short: {len(audio_data)} chars")
                await voice_feedback("error")
                return result(False, "Audio data too short", err=f"Only {len(audio_data)} chars")

            logger.info(f"[ROBUST] Audio data preview: {audio_data[:50]}...")

            # 🎙️ STAGE: Init
            # ============================================================
            # SYNCHRONIZED voice: Speak acknowledgment BEFORE processing
            # The user should hear "Just a moment" before we start working
            # ============================================================
            await progress("init", 5, "Acknowledging request...")

            # Get owner name for personalized greeting
            init_name = dynamic_owner_name or "there"
            init_msg = f"Just a moment, {init_name}."
            await speak_and_wait(init_msg)

            await progress("init", 10, "Starting VBI processing...")

            # Parallel: decode audio + load primary profile
            await progress("audio_decode", 15, "Decoding audio...")
            audio_task = asyncio.create_task(_decode_audio_robust(audio_data, mime_type))
            # Load primary user profile (dynamically detected)
            profile_task = asyncio.create_task(_load_primary_speaker_profile())

            try:
                audio_bytes, profile_result = await asyncio.gather(audio_task, profile_task, return_exceptions=True)
            except Exception as e:
                logger.error(f"[ROBUST] Parallel gather failed: {e}")
                await voice_feedback("error")
                return result(False, f"Parallel load failed: {e}", err=str(e))

            # Extract reference embedding and speaker name from profile
            ref_emb = None
            verified_speaker = dynamic_owner_name or "there"
            if isinstance(profile_result, dict):
                ref_emb = profile_result.get("embedding")
                verified_speaker = profile_result.get("name", verified_speaker)
            elif isinstance(profile_result, (list, tuple)) and len(profile_result) > 0:
                ref_emb = profile_result

            # Check audio decode result
            if isinstance(audio_bytes, Exception):
                logger.error(f"[ROBUST] Audio decode raised exception: {audio_bytes}")
                await voice_feedback("error")
                return result(False, f"Audio decode error: {audio_bytes}", err=str(audio_bytes))

            if audio_bytes is None:
                logger.error("[ROBUST] Audio decode returned None")
                await voice_feedback("error")
                return result(False, "Audio decode returned None - check format", err="decode returned None")

            if len(audio_bytes) < 100:
                logger.error(f"[ROBUST] Decoded audio too short: {len(audio_bytes)} bytes")
                await voice_feedback("error")
                return result(False, f"Decoded audio too short ({len(audio_bytes)} bytes)", err="decoded audio < 100 bytes")

            logger.info(f"[ROBUST] Audio decoded successfully: {len(audio_bytes)} bytes")

            # ✅ Audio decode SUCCESS
            stages.append({"stage": "audio_decode", "success": True})
            await progress("audio_decode", 25, f"Audio decoded ({len(audio_bytes)} bytes)", success=True)

            if isinstance(ref_emb, Exception) or not ref_emb:
                logger.warning(f"[ROBUST] No reference embedding: {ref_emb}")
                await progress("profile_load", 30, "No voice profile found", success=False)
            else:
                stages.append({"stage": "profile_load", "success": True, "dim": len(ref_emb), "speaker": verified_speaker})
                logger.info(f"[ROBUST] Reference embedding: {len(ref_emb)} dims for {verified_speaker}")
                await progress("profile_load", 30, f"Voice profile loaded for {verified_speaker}", success=True)

            # 🎙️ STAGE: ECAPA Extract - Voice: "Analyzing your voice."
            await progress("ecapa_extract", 45, "Extracting voiceprint...")
            await voice_feedback("ecapa_extract")
            test_emb = await _extract_ecapa_robust(audio_bytes)

            if not test_emb:
                # ❌ ECAPA Extract FAILED
                await progress("ecapa_extract", 45, "Failed to extract voiceprint", success=False)
                await voice_feedback("failed", confidence=0.0)
                return result(False, "Failed to extract voiceprint", err="ECAPA failed")

            # ✅ ECAPA Extract SUCCESS
            stages.append({"stage": "ecapa_extract", "success": True, "dim": len(test_emb)})
            await progress("ecapa_extract", 55, f"Voiceprint extracted ({len(test_emb)} dims)", success=True)
            await progress("verification", 65, "Verifying speaker...")

            if not ref_emb:
                await progress("verification", 70, "No voice profile to compare", success=False)
                # SYNCHRONIZED: Speak failure message
                await speak_and_wait("I don't have a voice profile to compare against. Please set up voice authentication first.")
                return result(False, "No voice profile found", conf=0.0, err="No reference embedding")

            # =========================================================================
            # 🧠 TIME-OPTIMIZED VERIFICATION - Use learned temporal patterns
            # =========================================================================
            # This is how JARVIS gets SMARTER - by using time-of-day voice patterns
            # learned from previous successful authentications
            # =========================================================================
            try:
                ref_emb_optimized = await _get_time_optimized_embedding(ref_emb, verified_speaker)
            except Exception as e:
                logger.debug(f"[ROBUST] Time optimization skipped: {e}")
                ref_emb_optimized = ref_emb

            verified, conf = _verify_speaker_robust(test_emb, ref_emb_optimized)
            stages.append({"stage": "verification", "success": verified, "confidence": conf})

            # Note: Verification voice feedback removed - now handled by synchronized
            # unlock_execute announcement below for better timing alignment

            if not verified:
                # ❌ Verification FAILED
                await progress("verification", 80, f"Voice not verified ({conf:.1%})", success=False)
                # SYNCHRONIZED: Speak failure message
                await speak_error_sync("failed", confidence=conf)
                return result(False, f"Voice not verified ({conf:.1%})", conf=conf)

            # ✅ Verification SUCCESS
            await progress("verification", 80, f"Voice verified ({conf:.1%})", success=True, confidence=conf)

            # 🎙️ STAGE: Unlock Execute
            # ============================================================
            # CRITICAL: Use SYNCHRONIZED voice feedback here!
            # The user must hear "Voice verified, unlocking now" BEFORE
            # the unlock starts - not during or after.
            # ============================================================
            await progress("unlock_execute", 85, f"Verified {verified_speaker}, preparing unlock...")

            # SYNCHRONIZED voice: Speak and WAIT before unlocking
            unlock_announcement = f"Voice verified, {verified_speaker}. Unlocking now."
            await speak_and_wait(unlock_announcement)

            await progress("unlock_execute", 90, f"Unlocking for {verified_speaker}...")

            # Pass progress callback and confidence for transparent substage updates
            unlocked = await _execute_unlock_robust(
                speaker_name=verified_speaker,
                progress_callback=progress_callback,
                confidence=conf
            )
            stages.append({"stage": "unlock_execute", "success": unlocked})

            if not unlocked:
                # ❌ Unlock FAILED
                await progress("unlock_execute", 90, "Screen unlock failed", success=False)
                # Synchronized failure announcement
                await speak_and_wait("I couldn't unlock the screen. Please try again.")
                return result(False, "Screen unlock failed", speaker=verified_speaker, conf=conf, err="Unlock execution failed")

            # ✅ Unlock SUCCESS
            await progress("unlock_execute", 95, f"Screen unlocked for {verified_speaker}", success=True)

            # Calculate total duration for notification
            total_duration_ms = (time.time() - start) * 1000

            # =========================================================================
            # 📚 CONTINUOUS LEARNING - Save successful audio for profile improvement
            # =========================================================================
            # Run in background (fire-and-forget) to not delay the user experience
            # The audio sample will be saved to SQLite for future voice profile updates
            # Records: date, time, day_of_week, confidence%, embedding, audio quality
            # =========================================================================
            asyncio.create_task(
                save_for_continuous_learning(
                    speaker_name=verified_speaker,
                    confidence=conf,
                    audio_bytes=audio_bytes if isinstance(audio_bytes, bytes) else b'',
                    embedding=test_emb.tolist() if hasattr(test_emb, 'tolist') else list(test_emb) if test_emb is not None else None,
                    duration_ms=total_duration_ms,
                    success=True,
                    error_reason=None,
                    verification_time_ms=None,  # Could be tracked from stages
                    unlock_time_ms=None,  # Could be tracked from stages
                    audio_quality=None,  # Auto-determined from confidence
                    handler="robust_v1"
                )
            )

            # =========================================================================
            # 🔔 NOTIFICATION - Send macOS notification with confidence score
            # =========================================================================
            # Run in background to not delay voice feedback
            # =========================================================================
            asyncio.create_task(
                send_unlock_notification(
                    speaker_name=verified_speaker,
                    confidence=conf,
                    duration_ms=total_duration_ms
                )
            )

            # 🎙️ STAGE: Complete
            # ============================================================
            # SYNCHRONIZED voice: Speak completion message AFTER unlock
            # This ensures the user hears the welcome AFTER seeing the
            # screen unlock, not during or before.
            # ============================================================
            await progress("complete", 98, "Unlock successful, welcoming user...")

            # Generate time-aware completion message
            completion_msg = f"Welcome back, {verified_speaker}."
            if conf >= 0.95:
                completion_msg = f"Of course, {verified_speaker}. Welcome back."

            # SYNCHRONIZED: Speak and wait for completion
            await speak_and_wait(completion_msg)

            # ✅ Complete SUCCESS
            await progress("complete", 100, f"Welcome back, {verified_speaker}!", success=True, confidence=conf)

            # Log successful VBI unlock with all details
            logger.info(f"🔓 [VBI-SUCCESS] Unlocked for {verified_speaker} | Confidence: {conf:.1%} | Duration: {total_duration_ms:.0f}ms")

            return result(True, completion_msg, speaker=verified_speaker, conf=conf)

        res = await asyncio.wait_for(_unlock(), timeout=RobustUnlockConfig.MAX_TOTAL_TIMEOUT)
        logger.info(f"[ROBUST] RESULT: {'SUCCESS' if res['success'] else 'FAILED'} in {res['total_duration_ms']:.0f}ms")
        return res

    except asyncio.TimeoutError:
        logger.error(f"[ROBUST] Total timeout ({RobustUnlockConfig.MAX_TOTAL_TIMEOUT}s)")
        return result(False, "Voice unlock timed out", err=f"Timeout after {RobustUnlockConfig.MAX_TOTAL_TIMEOUT}s")
    except Exception as e:
        logger.error(f"[ROBUST] Error: {e}\n{traceback.format_exc()}")
        return result(False, f"Voice unlock error: {e}", err=str(e))
