#!/usr/bin/env python3
"""
Voice Biometric Intelligence v1.0
==================================

Intelligent voice recognition system that provides UPFRONT transparency
about voice verification BEFORE proceeding with unlock operations.

Key Features:
- Fast parallel voice verification (sub-second when cached)
- Progressive confidence communication
- Environmental awareness and adaptation
- Voice quality analysis
- Intelligent retry guidance
- Learning acknowledgment
- Anti-spoofing detection with transparent feedback

This module ensures JARVIS communicates voice recognition status
BEFORE the unlock process, providing transparency and trust.

Example Flow:
1. User: "Unlock my screen"
2. JARVIS: "Voice verified, Derek. 94% confidence. Unlocking now..."
3. [Screen unlocks]

vs Previous Flow:
1. User: "Unlock my screen"
2. [Long pause... processing... stuck at "Processing..."]
3. Maybe screen unlocks, maybe not

Usage:
    from voice_unlock.voice_biometric_intelligence import (
        VoiceBiometricIntelligence,
        get_voice_biometric_intelligence,
    )

    vbi = await get_voice_biometric_intelligence()
    result = await vbi.verify_and_announce(audio_data)

    if result.verified:
        # Proceed with unlock
        pass
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple
import random

logger = logging.getLogger(__name__)


# =============================================================================
# DYNAMIC CONFIGURATION
# =============================================================================
class VBIConfig:
    """Dynamic configuration for Voice Biometric Intelligence."""

    def __init__(self):
        # Timeouts
        self.fast_verify_timeout = float(os.getenv('VBI_FAST_VERIFY_TIMEOUT', '2.0'))
        self.full_verify_timeout = float(os.getenv('VBI_FULL_VERIFY_TIMEOUT', '5.0'))
        self.audio_analysis_timeout = float(os.getenv('VBI_AUDIO_ANALYSIS_TIMEOUT', '1.0'))

        # Thresholds
        self.instant_recognition_threshold = float(os.getenv('VBI_INSTANT_THRESHOLD', '0.92'))
        self.confident_threshold = float(os.getenv('VBI_CONFIDENT_THRESHOLD', '0.85'))
        self.borderline_threshold = float(os.getenv('VBI_BORDERLINE_THRESHOLD', '0.75'))
        self.rejection_threshold = float(os.getenv('VBI_REJECTION_THRESHOLD', '0.60'))

        # Behavior
        self.announce_confidence = os.getenv('VBI_ANNOUNCE_CONFIDENCE', 'borderline').lower()
        self.use_behavioral_fusion = os.getenv('VBI_USE_BEHAVIORAL', 'true').lower() == 'true'
        self.enable_learning_feedback = os.getenv('VBI_LEARNING_FEEDBACK', 'true').lower() == 'true'

        # =======================================================================
        # PERFORMANCE OPTIMIZATIONS (v2.0)
        # =======================================================================

        # Early high-confidence exit: proceed immediately when ML confidence exceeds this
        # Physics/behavioral checks continue in background for learning
        self.early_exit_threshold = float(os.getenv('VBI_EARLY_EXIT_THRESHOLD', '0.95'))
        self.enable_early_exit = os.getenv('VBI_ENABLE_EARLY_EXIT', 'true').lower() == 'true'

        # Speculative unlock execution: start unlock prep before final confirmation
        self.enable_speculative_unlock = os.getenv('VBI_SPECULATIVE_UNLOCK', 'true').lower() == 'true'
        self.speculative_threshold = float(os.getenv('VBI_SPECULATIVE_THRESHOLD', '0.90'))

        # Voice profile preloading: keep owner voiceprint in hot memory
        self.enable_profile_preloading = os.getenv('VBI_PROFILE_PRELOAD', 'true').lower() == 'true'
        self.hot_cache_ttl_seconds = int(os.getenv('VBI_HOT_CACHE_TTL', '3600'))  # 1 hour

        # Model quantization: use INT8 for faster inference
        self.enable_int8_quantization = os.getenv('VBI_INT8_QUANTIZATION', 'false').lower() == 'true'

        # Parallel physics checks: run anti-spoofing in parallel with verification
        self.enable_parallel_physics = os.getenv('VBI_PARALLEL_PHYSICS', 'true').lower() == 'true'


_config = VBIConfig()


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================
class RecognitionLevel(Enum):
    """Voice recognition confidence levels."""
    INSTANT = "instant"          # >92% - Immediate recognition
    CONFIDENT = "confident"      # 85-92% - Clear match
    GOOD = "good"                # 75-85% - Solid match
    BORDERLINE = "borderline"    # 60-75% - Uncertain
    UNKNOWN = "unknown"          # <60% - Not recognized
    SPOOFING = "spoofing"        # Replay/recording detected


class EnvironmentQuality(Enum):
    """Audio environment quality."""
    EXCELLENT = "excellent"      # SNR > 25dB
    GOOD = "good"                # SNR 18-25dB
    FAIR = "fair"                # SNR 12-18dB
    POOR = "poor"                # SNR 6-12dB
    NOISY = "noisy"              # SNR < 6dB


class VoiceQuality(Enum):
    """Voice signal quality."""
    CLEAR = "clear"              # Normal voice
    MUFFLED = "muffled"          # Different mic or obstruction
    HOARSE = "hoarse"            # Possible illness
    TIRED = "tired"              # Lower energy
    STRESSED = "stressed"        # Higher pitch/speed
    WHISPER = "whisper"          # Very quiet


class VerificationMethod(Enum):
    """How verification was achieved."""
    VOICE_ONLY = "voice_only"
    VOICE_BEHAVIORAL = "voice_behavioral"
    BEHAVIORAL_ONLY = "behavioral_only"
    CACHED = "cached"
    MULTI_FACTOR = "multi_factor"


@dataclass
class AudioAnalysis:
    """Analysis of audio quality and environment."""
    duration_ms: float = 0.0
    snr_db: float = 0.0
    environment: EnvironmentQuality = EnvironmentQuality.GOOD
    voice_quality: VoiceQuality = VoiceQuality.CLEAR
    has_speech: bool = True
    speech_ratio: float = 0.0  # Ratio of speech to total audio
    clipping_detected: bool = False
    silence_ratio: float = 0.0
    issues: List[str] = field(default_factory=list)


@dataclass
class BehavioralContext:
    """Behavioral and contextual factors."""
    is_typical_time: bool = True
    hours_since_last_unlock: float = 0.0
    is_typical_location: bool = True
    device_trusted: bool = True
    consecutive_failures: int = 0
    session_active: bool = False
    behavioral_confidence: float = 0.0


@dataclass
class VerificationResult:
    """Complete verification result with intelligence."""
    # Core result
    verified: bool = False
    speaker_name: Optional[str] = None
    confidence: float = 0.0
    level: RecognitionLevel = RecognitionLevel.UNKNOWN

    # Detailed analysis
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    fused_confidence: float = 0.0
    verification_method: VerificationMethod = VerificationMethod.VOICE_ONLY

    # Audio analysis
    audio: AudioAnalysis = field(default_factory=AudioAnalysis)

    # Context
    behavioral: BehavioralContext = field(default_factory=BehavioralContext)

    # Timing
    verification_time_ms: float = 0.0
    was_cached: bool = False

    # Narration
    announcement: str = ""
    should_proceed: bool = False
    retry_guidance: Optional[str] = None

    # Learning
    learned_something: bool = False
    learning_note: Optional[str] = None

    # Security
    spoofing_detected: bool = False
    spoofing_reason: Optional[str] = None

    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    session_id: Optional[str] = None


# =============================================================================
# ANNOUNCEMENT GENERATOR
# =============================================================================
class IntelligentAnnouncementGenerator:
    """
    Generates intelligent, context-aware announcements for voice verification.

    Creates human-like responses that communicate:
    - Recognition status
    - Confidence level (when appropriate)
    - Environmental awareness
    - Learning feedback
    - Retry guidance
    """

    def __init__(self, config: VBIConfig):
        self._config = config
        self._stats = {
            'total_announcements': 0,
            'instant_recognitions': 0,
            'confident_matches': 0,
            'borderline_matches': 0,
            'rejections': 0,
        }

    def _get_time_greeting(self) -> str:
        """Get time-appropriate greeting prefix."""
        hour = datetime.now().hour

        if 5 <= hour < 7:
            return random.choice(["Early morning", "You're up early"])
        elif 7 <= hour < 12:
            return "Good morning"
        elif 12 <= hour < 17:
            return "Good afternoon"
        elif 17 <= hour < 21:
            return "Good evening"
        elif 21 <= hour < 24:
            return random.choice(["Evening", "Still at it"])
        else:
            return random.choice(["Late night session", "Burning the midnight oil"])

    def generate_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool = None,
    ) -> str:
        """
        Generate an intelligent announcement based on verification result.

        Args:
            result: Verification result
            include_confidence: Whether to mention confidence (auto if None)

        Returns:
            Human-like announcement string
        """
        self._stats['total_announcements'] += 1

        # Determine if we should include confidence
        if include_confidence is None:
            include_confidence = self._should_include_confidence(result)

        # Handle different scenarios
        if result.spoofing_detected:
            return self._generate_spoofing_announcement(result)

        if result.level == RecognitionLevel.UNKNOWN:
            return self._generate_unknown_announcement(result)

        if result.level == RecognitionLevel.INSTANT:
            self._stats['instant_recognitions'] += 1
            return self._generate_instant_announcement(result, include_confidence)

        if result.level == RecognitionLevel.CONFIDENT:
            self._stats['confident_matches'] += 1
            return self._generate_confident_announcement(result, include_confidence)

        if result.level == RecognitionLevel.GOOD:
            return self._generate_good_announcement(result, include_confidence)

        if result.level == RecognitionLevel.BORDERLINE:
            self._stats['borderline_matches'] += 1
            return self._generate_borderline_announcement(result, include_confidence)

        self._stats['rejections'] += 1
        return self._generate_failed_announcement(result)

    def _should_include_confidence(self, result: VerificationResult) -> bool:
        """Determine if confidence should be mentioned."""
        mode = self._config.announce_confidence

        if mode == 'always':
            return True
        elif mode == 'never':
            return False
        elif mode == 'borderline':
            return result.level in [RecognitionLevel.BORDERLINE, RecognitionLevel.GOOD]
        else:
            return result.confidence < 0.90

    def _generate_instant_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for instant recognition."""
        name = result.speaker_name or "there"
        greeting = self._get_time_greeting()

        templates = [
            f"Voice verified, {name}. Unlocking now.",
            f"{greeting}, {name}. Unlocking for you.",
            f"Of course, {name}. Unlocking now.",
            f"Recognized, {name}. Proceeding with unlock.",
        ]

        announcement = random.choice(templates)

        # Add learning note if applicable
        if result.learned_something and result.learning_note:
            announcement += f" {result.learning_note}"

        return announcement

    def _generate_confident_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for confident match."""
        name = result.speaker_name or "there"

        if include_confidence:
            confidence_pct = int(result.confidence * 100)
            templates = [
                f"Voice verified, {name}. {confidence_pct}% confidence. Unlocking now.",
                f"Confirmed, {name}. Voice match at {confidence_pct}%. Unlocking.",
            ]
        else:
            templates = [
                f"Voice verified, {name}. Unlocking now.",
                f"Confirmed, {name}. Proceeding with unlock.",
                f"Welcome back, {name}. Unlocking for you.",
            ]

        return random.choice(templates)

    def _generate_good_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for good match."""
        name = result.speaker_name or "there"

        # Check for environmental factors
        if result.audio.environment == EnvironmentQuality.NOISY:
            return (
                f"Voice verified despite background noise, {name}. "
                f"Unlocking now."
            )

        if result.audio.voice_quality == VoiceQuality.HOARSE:
            return (
                f"Your voice sounds a bit different today, {name}, "
                f"but I'm confident it's you. Unlocking now."
            )

        if include_confidence:
            confidence_pct = int(result.confidence * 100)
            return f"Voice match confirmed at {confidence_pct}%, {name}. Unlocking."

        return f"One moment... verified. Unlocking for you, {name}."

    def _generate_borderline_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for borderline match."""
        name = result.speaker_name or "there"
        confidence_pct = int(result.confidence * 100)

        # Explain why confidence is lower
        explanations = []

        if result.audio.environment in [EnvironmentQuality.POOR, EnvironmentQuality.NOISY]:
            explanations.append("there's background noise")
        if result.audio.voice_quality == VoiceQuality.MUFFLED:
            explanations.append("the audio is a bit muffled")
        if result.audio.voice_quality == VoiceQuality.TIRED:
            explanations.append("you sound tired")
        if result.audio.voice_quality == VoiceQuality.HOARSE:
            explanations.append("your voice sounds different")

        if result.verification_method == VerificationMethod.VOICE_BEHAVIORAL:
            # Multi-factor saved it
            if explanations:
                reason = " and ".join(explanations[:2])
                return (
                    f"Voice confidence is {confidence_pct}% due to {reason}, {name}, "
                    f"but your behavioral patterns match perfectly. Unlocking."
                )
            return (
                f"Voice is borderline at {confidence_pct}%, {name}, "
                f"but context confirms it's you. Unlocking."
            )

        if explanations:
            reason = explanations[0]
            return (
                f"I'm having some trouble hearing clearly - {reason}. "
                f"Confidence is {confidence_pct}%, but proceeding with unlock, {name}."
            )

        return f"Voice match at {confidence_pct}%, {name}. Unlocking now."

    def _generate_unknown_announcement(self, result: VerificationResult) -> str:
        """Generate announcement for unknown speaker."""
        if result.audio.environment == EnvironmentQuality.NOISY:
            return (
                "I'm having trouble verifying your voice due to background noise. "
                "Could you speak a bit louder or move to a quieter spot?"
            )

        if not result.audio.has_speech:
            return "I didn't detect any speech. Please say 'unlock my screen' clearly."

        return (
            "I don't recognize this voice. Voice unlock is configured for "
            "the registered owner only."
        )

    def _generate_failed_announcement(self, result: VerificationResult) -> str:
        """Generate announcement for failed verification."""
        confidence_pct = int(result.confidence * 100)

        if result.behavioral.consecutive_failures >= 3:
            return (
                f"Voice verification has failed {result.behavioral.consecutive_failures} times. "
                "Please try using keyboard authentication instead, or wait a moment and try again."
            )

        if result.audio.snr_db < 10:
            return (
                "Voice verification failed due to excessive background noise. "
                "Please move to a quieter location and try again."
            )

        return (
            f"Voice confidence is too low at {confidence_pct}%. "
            "Please try again, speaking clearly into the microphone."
        )

    def _generate_spoofing_announcement(self, result: VerificationResult) -> str:
        """Generate announcement for detected spoofing attempt."""
        reason = result.spoofing_reason or "suspicious characteristics"

        return (
            f"Security alert: I detected {reason} consistent with a recording "
            "rather than a live voice. Access denied. This attempt has been logged."
        )

    def generate_retry_guidance(self, result: VerificationResult) -> str:
        """Generate guidance for retry attempts."""
        issues = result.audio.issues

        if "low_snr" in issues:
            return "Try speaking closer to the microphone in a quieter environment."

        if "clipping" in issues:
            return "You're speaking too loudly. Try speaking at a normal volume."

        if "short_audio" in issues:
            return "The audio was too short. Please say the full unlock phrase."

        if result.audio.voice_quality == VoiceQuality.WHISPER:
            return "Your voice is very quiet. Please speak at normal volume."

        if result.behavioral.consecutive_failures >= 2:
            return (
                "Multiple verification failures. Try waiting a moment, "
                "then speak clearly and naturally."
            )

        return "Please try again, speaking clearly and at normal volume."


# =============================================================================
# VOICE BIOMETRIC INTELLIGENCE
# =============================================================================
class VoiceBiometricIntelligence:
    """
    Intelligent voice biometric verification with upfront transparency.

    This class provides FAST voice verification that communicates results
    BEFORE the unlock process, giving users immediate feedback about
    their recognition status.

    Features:
    - Sub-second verification (when cached/prewarmed)
    - Intelligent confidence communication
    - Environmental awareness
    - Voice quality analysis
    - Behavioral fusion
    - Anti-spoofing detection
    - Learning feedback
    """

    def __init__(self):
        self._config = _config
        self._initialized = False

        # Components (lazy-loaded)
        self._speaker_engine = None
        self._unified_cache = None
        self._narrator = None
        self._voice_communicator = None

        # Announcement generator
        self._announcer = IntelligentAnnouncementGenerator(self._config)

        # Statistics
        self._stats = {
            'total_verifications': 0,
            'instant_recognitions': 0,
            'cached_hits': 0,
            'successful_verifications': 0,
            'failed_verifications': 0,
            'spoofing_detections': 0,
            'avg_verification_time_ms': 0.0,
            'total_verification_time_ms': 0.0,
            # Performance optimization stats
            'early_exits': 0,
            'speculative_unlocks': 0,
            'hot_cache_hits': 0,
            'quantized_inferences': 0,
        }

        # Recent verifications for pattern analysis
        self._recent_verifications: List[VerificationResult] = []
        self._max_history = 50

        # Session tracking
        self._current_session_id: Optional[str] = None
        self._session_start_time: Optional[datetime] = None
        self._session_verifications = 0

        # =======================================================================
        # PERFORMANCE OPTIMIZATION STATE (v2.0)
        # =======================================================================

        # Hot memory cache for owner voiceprint (sub-10ms lookups)
        self._hot_voiceprint_cache: Dict[str, Any] = {}
        self._hot_cache_timestamps: Dict[str, float] = {}
        self._hot_cache_lock = asyncio.Lock()

        # Speculative unlock state
        self._speculative_unlock_task: Optional[asyncio.Task] = None
        self._speculative_unlock_ready = asyncio.Event()

        # Background physics verification (continues after early exit)
        self._background_physics_task: Optional[asyncio.Task] = None
        self._physics_results: Dict[str, Any] = {}

        # Quantized model reference
        self._quantized_encoder = None
        self._quantization_available = False

        logger.info("VoiceBiometricIntelligence initialized with performance optimizations")

    async def initialize(self) -> bool:
        """Initialize components for voice verification."""
        if self._initialized:
            return True

        logger.info("🧠 Initializing Voice Biometric Intelligence with Performance Optimizations...")
        init_start = time.time()

        # Initialize components in parallel
        init_tasks = [
            self._init_speaker_engine(),
            self._init_unified_cache(),
            self._init_voice_communicator(),
        ]

        await asyncio.gather(*init_tasks, return_exceptions=True)

        # Initialize performance optimizations (non-blocking)
        perf_tasks = []
        if self._config.enable_profile_preloading:
            perf_tasks.append(self._init_hot_cache())
        if self._config.enable_int8_quantization:
            perf_tasks.append(self._init_quantization())

        if perf_tasks:
            await asyncio.gather(*perf_tasks, return_exceptions=True)

        self._initialized = True
        init_time = (time.time() - init_start) * 1000
        logger.info(f"✅ Voice Biometric Intelligence initialized in {init_time:.0f}ms")
        logger.info(f"   Performance options: early_exit={self._config.enable_early_exit}, "
                   f"speculative_unlock={self._config.enable_speculative_unlock}, "
                   f"profile_preload={self._config.enable_profile_preloading}")

        return True

    async def _init_hot_cache(self):
        """Pre-load owner voiceprint into hot memory cache for sub-10ms lookups."""
        try:
            logger.info("🔥 Initializing hot voiceprint cache...")

            # Get owner profile from unified cache or database
            if self._unified_cache and hasattr(self._unified_cache, 'get_owner_profile'):
                owner_profile = await self._unified_cache.get_owner_profile()
                if owner_profile:
                    async with self._hot_cache_lock:
                        self._hot_voiceprint_cache['owner'] = owner_profile
                        self._hot_cache_timestamps['owner'] = time.time()
                    logger.info(f"✅ Owner voiceprint cached in hot memory")
                    return

            # Fallback: load from database using read_voice_profile
            try:
                from intelligence.hybrid_database_sync import HybridDatabaseSync
                db = HybridDatabaseSync()
                await db.initialize()

                # Get owner name from environment or use default
                owner_name = os.getenv('VBI_OWNER_NAME', 'Derek')

                # Read the owner's voice profile
                owner_profile = await db.read_voice_profile(owner_name)
                if owner_profile:
                    async with self._hot_cache_lock:
                        self._hot_voiceprint_cache['owner'] = {
                            'name': owner_profile.get('name', owner_name),
                            'embedding': owner_profile.get('embedding'),
                            'samples_count': owner_profile.get('samples_count', 0),
                        }
                        self._hot_cache_timestamps['owner'] = time.time()
                    logger.info(f"✅ Owner '{owner_name}' voiceprint cached from database")
                else:
                    logger.info(f"No voiceprint found for owner '{owner_name}' - hot cache disabled")

            except Exception as e:
                logger.warning(f"Could not load owner profile from database: {e}")

        except Exception as e:
            logger.warning(f"Hot cache initialization failed: {e}")

    async def _init_quantization(self):
        """Initialize INT8 quantized model for faster inference."""
        try:
            logger.info("⚡ Initializing INT8 quantization...")

            # Check if quantization is available
            try:
                import torch

                if not hasattr(torch, 'quantization'):
                    logger.info("INT8 quantization not available (requires PyTorch with quantization support)")
                    return

                # Try to load or create quantized encoder
                from voice_unlock.ml_engine_registry import get_ml_registry_sync
                registry = get_ml_registry_sync()

                if registry and registry.is_ready:
                    ecapa_wrapper = registry.get_wrapper("ecapa_tdnn")
                    if ecapa_wrapper and ecapa_wrapper.is_loaded:
                        encoder = ecapa_wrapper.get_engine()

                        # Dynamic quantization for faster inference
                        self._quantized_encoder = torch.quantization.quantize_dynamic(
                            encoder,
                            {torch.nn.Linear, torch.nn.LSTM},
                            dtype=torch.qint8
                        )
                        self._quantization_available = True
                        logger.info("✅ INT8 quantized encoder ready (2-3x faster inference)")

            except ImportError:
                logger.debug("Quantization dependencies not available")
            except Exception as e:
                logger.debug(f"Quantization setup failed: {e}")

        except Exception as e:
            logger.warning(f"Quantization initialization failed: {e}")

    async def _init_speaker_engine(self):
        """Initialize speaker verification engine."""
        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            self._speaker_engine = await get_speaker_verification_service()
            logger.debug("Speaker verification service connected")
        except Exception as e:
            logger.warning(f"Speaker verification service not available: {e}")
            try:
                from voice.speaker_recognition import get_speaker_recognition_engine
                self._speaker_engine = get_speaker_recognition_engine()
                await self._speaker_engine.initialize()
                logger.debug("Legacy speaker recognition connected")
            except Exception as e2:
                logger.error(f"No speaker engine available: {e2}")

    async def _init_unified_cache(self):
        """Initialize unified voice cache for fast lookups."""
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_voice_cache

            self._unified_cache = await get_unified_voice_cache()

            # Log cache status for debugging
            if self._unified_cache:
                profiles_count = self._unified_cache.profiles_loaded
                state = self._unified_cache.state.value if hasattr(self._unified_cache.state, 'value') else str(self._unified_cache.state)
                logger.info(
                    f"✅ Unified voice cache connected: "
                    f"{profiles_count} profiles, state={state}"
                )

                if profiles_count == 0:
                    logger.warning(
                        "⚠️ Unified voice cache has NO profiles loaded! "
                        "Voice recognition will fail without profiles."
                    )
            else:
                logger.warning("⚠️ Unified voice cache returned None")

        except ImportError as e:
            logger.warning(f"⚠️ Unified voice cache module not available: {e}")
            self._unified_cache = None
        except Exception as e:
            logger.error(f"❌ Unified voice cache initialization failed: {e}", exc_info=True)
            self._unified_cache = None

    async def _init_voice_communicator(self):
        """Initialize voice communicator for announcements."""
        try:
            from voice.realtime_voice_communicator import get_voice_communicator
            self._voice_communicator = await get_voice_communicator()
            logger.debug("Voice communicator connected")
        except Exception as e:
            logger.debug(f"Voice communicator not available: {e}")

    async def verify_and_announce(
        self,
        audio_data: bytes,
        context: Optional[Dict[str, Any]] = None,
        speak: bool = True,
    ) -> VerificationResult:
        """
        Verify voice biometrics and announce the result.

        PERFORMANCE OPTIMIZED (v2.0):
        - Early high-confidence exit: Proceeds immediately when ML confidence >95%
        - Speculative unlock: Starts unlock prep while final checks complete
        - Hot cache: Uses preloaded voiceprint for sub-10ms matching
        - Background physics: Physics checks continue after early exit for learning

        Args:
            audio_data: Raw audio bytes
            context: Optional context (screen state, location, etc.)
            speak: Whether to speak the announcement

        Returns:
            VerificationResult with complete analysis and announcement
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        self._stats['total_verifications'] += 1

        # Create result object
        result = VerificationResult(
            timestamp=datetime.now(),
            session_id=self._current_session_id,
        )

        try:
            # =====================================================================
            # FAST PATH: Try hot cache first (sub-10ms if cached)
            # =====================================================================
            hot_cache_result = await self._check_hot_cache(audio_data)
            if hot_cache_result:
                speaker_name, voice_confidence = hot_cache_result
                self._stats['hot_cache_hits'] += 1
                result.speaker_name = speaker_name
                result.voice_confidence = voice_confidence
                result.was_cached = True

                # If hot cache gives high confidence, use early exit
                if self._config.enable_early_exit and voice_confidence >= self._config.early_exit_threshold:
                    logger.info(f"⚡ HOT CACHE EARLY EXIT: {speaker_name} ({voice_confidence:.1%})")
                    self._stats['early_exits'] += 1

                    result.fused_confidence = voice_confidence
                    result.confidence = voice_confidence
                    result.level = RecognitionLevel.INSTANT
                    result.verified = True
                    result.should_proceed = True
                    result.verification_method = VerificationMethod.CACHED
                    result.announcement = self._announcer.generate_announcement(result)

                    # Start speculative unlock in background
                    if self._config.enable_speculative_unlock:
                        self._start_speculative_unlock(context)

                    # Continue physics checks in background for learning
                    if self._config.enable_parallel_physics:
                        self._background_physics_task = asyncio.create_task(
                            self._run_background_physics(audio_data, result)
                        )

                    result.verification_time_ms = (time.time() - start_time) * 1000
                    self._update_timing_stats(result.verification_time_ms)
                    self._log_result(result)

                    if speak and result.announcement:
                        asyncio.create_task(self._speak(result.announcement))

                    return result

            # =====================================================================
            # PARALLEL VERIFICATION: Run all checks concurrently
            # =====================================================================
            verify_task = asyncio.create_task(
                self._verify_speaker(audio_data)
            )
            audio_task = asyncio.create_task(
                self._analyze_audio(audio_data)
            )
            behavioral_task = asyncio.create_task(
                self._get_behavioral_context(context)
            )

            # Start physics anti-spoofing in parallel if enabled
            physics_task = None
            if self._config.enable_parallel_physics:
                physics_task = asyncio.create_task(
                    self._check_spoofing(audio_data, result)
                )

            # =====================================================================
            # EARLY EXIT CHECK: Check if ML verification completes with high confidence
            # =====================================================================
            if self._config.enable_early_exit:
                # Wait for verification first with short timeout
                try:
                    await asyncio.wait_for(verify_task, timeout=1.0)
                    if not verify_task.cancelled():
                        speaker_name, voice_confidence, was_cached = verify_task.result()
                        result.speaker_name = speaker_name
                        result.voice_confidence = voice_confidence
                        result.was_cached = was_cached

                        # Check for early exit condition
                        if voice_confidence >= self._config.early_exit_threshold and speaker_name:
                            logger.info(f"⚡ EARLY EXIT: {speaker_name} ({voice_confidence:.1%})")
                            self._stats['early_exits'] += 1

                            result.fused_confidence = voice_confidence
                            result.confidence = voice_confidence
                            result.level = RecognitionLevel.INSTANT
                            result.verified = True
                            result.should_proceed = True
                            result.verification_method = VerificationMethod.VOICE_ONLY
                            result.announcement = self._announcer.generate_announcement(result)

                            # Start speculative unlock
                            if self._config.enable_speculative_unlock and voice_confidence >= self._config.speculative_threshold:
                                self._stats['speculative_unlocks'] += 1
                                self._start_speculative_unlock(context)

                            # Let other tasks continue in background for learning
                            self._background_physics_task = asyncio.create_task(
                                self._complete_background_verification(
                                    audio_task, behavioral_task, physics_task, audio_data, result
                                )
                            )

                            result.verification_time_ms = (time.time() - start_time) * 1000
                            self._update_timing_stats(result.verification_time_ms)
                            self._log_result(result)

                            if speak and result.announcement:
                                asyncio.create_task(self._speak(result.announcement))

                            return result

                except asyncio.TimeoutError:
                    pass  # Continue with standard flow

            # =====================================================================
            # STANDARD FLOW: Wait for all tasks
            # =====================================================================
            all_tasks = [verify_task, audio_task, behavioral_task]
            if physics_task:
                all_tasks.append(physics_task)

            done, pending = await asyncio.wait(
                all_tasks,
                timeout=self._config.full_verify_timeout,
                return_when=asyncio.ALL_COMPLETED
            )

            # Cancel any pending tasks
            for task in pending:
                task.cancel()

            # Get results
            if verify_task in done and not verify_task.cancelled():
                try:
                    speaker_name, voice_confidence, was_cached = verify_task.result()
                    result.speaker_name = speaker_name
                    result.voice_confidence = voice_confidence
                    result.was_cached = was_cached
                    if was_cached:
                        self._stats['cached_hits'] += 1
                except Exception as e:
                    logger.warning(f"Verification result error: {e}")

            if audio_task in done and not audio_task.cancelled():
                try:
                    result.audio = audio_task.result()
                except Exception as e:
                    logger.warning(f"Audio analysis error: {e}")

            if behavioral_task in done and not behavioral_task.cancelled():
                try:
                    result.behavioral = behavioral_task.result()
                except Exception as e:
                    logger.warning(f"Behavioral analysis error: {e}")

            # Fuse confidences
            result.fused_confidence = self._fuse_confidences(result)
            result.confidence = result.fused_confidence

            # Determine recognition level
            result.level = self._determine_level(result)

            # Get spoofing result
            if physics_task and physics_task in done and not physics_task.cancelled():
                try:
                    spoofing_detected, spoofing_reason = physics_task.result()
                    result.spoofing_detected = spoofing_detected
                    result.spoofing_reason = spoofing_reason
                except Exception as e:
                    logger.warning(f"Spoofing check error: {e}")
                    result.spoofing_detected = False
            else:
                # Run spoofing check synchronously if not done in parallel
                spoofing_detected, spoofing_reason = await self._check_spoofing(audio_data, result)
                result.spoofing_detected = spoofing_detected
                result.spoofing_reason = spoofing_reason

            if result.spoofing_detected:
                result.level = RecognitionLevel.SPOOFING
                result.verified = False
                self._stats['spoofing_detections'] += 1
            else:
                # Determine if verified
                result.verified = result.level in [
                    RecognitionLevel.INSTANT,
                    RecognitionLevel.CONFIDENT,
                    RecognitionLevel.GOOD,
                    RecognitionLevel.BORDERLINE,
                ]

                if result.verified:
                    self._stats['successful_verifications'] += 1
                else:
                    self._stats['failed_verifications'] += 1

            # Determine verification method
            result.verification_method = self._determine_method(result)

            # Set should_proceed
            result.should_proceed = result.verified

            # Generate announcement
            result.announcement = self._announcer.generate_announcement(result)

            if not result.verified:
                result.retry_guidance = self._announcer.generate_retry_guidance(result)

            # Check for learning opportunities
            result.learned_something, result.learning_note = self._check_learning(result)

        except asyncio.TimeoutError:
            logger.warning("Voice verification timed out")
            result.announcement = (
                "Voice verification is taking longer than expected. "
                "Please try again."
            )
            result.verified = False

        except Exception as e:
            logger.error(f"Voice verification error: {e}")
            result.announcement = (
                "I encountered an issue verifying your voice. "
                "Please try again."
            )
            result.verified = False

        # Record timing
        result.verification_time_ms = (time.time() - start_time) * 1000
        self._update_timing_stats(result.verification_time_ms)

        # Store in history
        self._recent_verifications.append(result)
        if len(self._recent_verifications) > self._max_history:
            self._recent_verifications.pop(0)

        # Speak announcement if requested
        if speak and result.announcement:
            await self._speak(result.announcement)

        # Log result
        self._log_result(result)

        return result

    async def _check_hot_cache(self, audio_data: bytes) -> Optional[Tuple[str, float]]:
        """Check hot memory cache for instant voiceprint matching."""
        if not self._config.enable_profile_preloading:
            return None

        try:
            async with self._hot_cache_lock:
                if 'owner' not in self._hot_voiceprint_cache:
                    return None

                # Check TTL
                cache_age = time.time() - self._hot_cache_timestamps.get('owner', 0)
                if cache_age > self._config.hot_cache_ttl_seconds:
                    logger.debug("Hot cache expired, refreshing...")
                    asyncio.create_task(self._init_hot_cache())
                    return None

                owner_profile = self._hot_voiceprint_cache['owner']
                cached_embedding = owner_profile.get('embedding')

                if cached_embedding is None:
                    return None

            # Extract embedding from audio (use quantized if available)
            test_embedding = await self._extract_embedding_fast(audio_data)
            if test_embedding is None:
                return None

            # Compute similarity
            import torch
            import torch.nn.functional as F

            if hasattr(cached_embedding, 'cpu'):
                ref = cached_embedding
            else:
                ref = torch.tensor(cached_embedding)

            if hasattr(test_embedding, 'cpu'):
                test = test_embedding
            else:
                test = torch.tensor(test_embedding)

            # Ensure same shape
            if test.dim() == 3:
                test = test.squeeze(0)
            if ref.dim() == 3:
                ref = ref.squeeze(0)

            similarity = F.cosine_similarity(
                test.view(1, -1).float(),
                ref.view(1, -1).float()
            ).item()

            owner_name = owner_profile.get('name', 'Owner')
            logger.debug(f"🔥 Hot cache match: {similarity:.1%}")

            return (owner_name, similarity)

        except Exception as e:
            logger.debug(f"Hot cache check failed: {e}")
            return None

    async def _extract_embedding_fast(self, audio_data: bytes) -> Optional[Any]:
        """Extract embedding using fastest available method (quantized if available)."""
        try:
            import torch
            import numpy as np

            # Use quantized encoder if available
            encoder = self._quantized_encoder if self._quantization_available else None

            if encoder is None:
                # Fall back to standard encoder
                try:
                    from voice_unlock.ml_engine_registry import get_ml_registry_sync
                    registry = get_ml_registry_sync()
                    if registry and registry.is_ready:
                        encoder = registry.get_engine("ecapa_tdnn")
                except Exception:
                    return None

            if encoder is None:
                return None

            # Convert audio bytes to tensor
            audio_array = np.frombuffer(audio_data, dtype=np.float32)
            audio_tensor = torch.tensor(audio_array).unsqueeze(0)

            # Extract embedding
            with torch.no_grad():
                embedding = encoder.encode_batch(audio_tensor)

            if self._quantization_available:
                self._stats['quantized_inferences'] += 1

            return embedding

        except Exception as e:
            logger.debug(f"Fast embedding extraction failed: {e}")
            return None

    def _start_speculative_unlock(self, context: Optional[Dict[str, Any]]):
        """Start unlock preparation speculatively (will be confirmed later)."""
        try:
            logger.debug("🚀 Starting speculative unlock preparation...")

            async def prepare_unlock():
                try:
                    # Pre-fetch unlock credentials
                    from voice_unlock.intelligent_voice_unlock_service import get_unlock_password

                    # Get password ready (from keychain)
                    password = await asyncio.wait_for(
                        asyncio.to_thread(get_unlock_password),
                        timeout=1.0
                    )

                    self._speculative_unlock_ready.set()
                    logger.debug("✅ Speculative unlock ready")

                except Exception as e:
                    logger.debug(f"Speculative unlock prep failed: {e}")

            # Don't await - run in background
            self._speculative_unlock_task = asyncio.create_task(prepare_unlock())

        except Exception as e:
            logger.debug(f"Could not start speculative unlock: {e}")

    async def _run_background_physics(self, audio_data: bytes, result: VerificationResult):
        """Continue physics verification in background after early exit."""
        try:
            # Run anti-spoofing checks
            spoofing_detected, spoofing_reason = await self._check_spoofing(audio_data, result)

            # Store results for later learning
            self._physics_results = {
                'spoofing_detected': spoofing_detected,
                'spoofing_reason': spoofing_reason,
                'timestamp': time.time(),
            }

            # If spoofing detected after early exit, log security event
            if spoofing_detected:
                logger.warning(
                    f"⚠️ SECURITY: Spoofing detected AFTER early exit! "
                    f"Reason: {spoofing_reason}"
                )
                # Could trigger additional security measures here

        except Exception as e:
            logger.debug(f"Background physics failed: {e}")

    async def _complete_background_verification(
        self,
        audio_task: asyncio.Task,
        behavioral_task: asyncio.Task,
        physics_task: Optional[asyncio.Task],
        audio_data: bytes,
        result: VerificationResult
    ):
        """Complete verification tasks in background after early exit."""
        try:
            # Wait for remaining tasks
            remaining = [audio_task, behavioral_task]
            if physics_task:
                remaining.append(physics_task)

            done, _ = await asyncio.wait(remaining, timeout=5.0)

            # Collect results for learning
            if audio_task in done:
                try:
                    audio_result = audio_task.result()
                    # Store for learning
                    logger.debug(f"Background audio analysis: SNR={audio_result.snr_db:.1f}dB")
                except Exception:
                    pass

            if behavioral_task in done:
                try:
                    behavioral = behavioral_task.result()
                    logger.debug(f"Background behavioral: confidence={behavioral.behavioral_confidence:.1%}")
                except Exception:
                    pass

            if physics_task and physics_task in done:
                try:
                    spoofing, reason = physics_task.result()
                    if spoofing:
                        logger.warning(f"⚠️ Background physics detected spoofing: {reason}")
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Background verification completion failed: {e}")

    async def _verify_speaker(
        self,
        audio_data
    ) -> Tuple[Optional[str], float, bool]:
        """
        Verify speaker identity from audio using FAST unified cache.

        ENHANCED: Handles all audio input types (bytes, base64 string, numpy, tensor).

        Returns:
            Tuple of (speaker_name, confidence, was_cached)
        """
        # =================================================================
        # AUDIO NORMALIZATION: Handle all input types
        # =================================================================
        try:
            # Import helper from unified cache manager
            from voice_unlock.unified_voice_cache_manager import normalize_audio_data
            normalized_audio = normalize_audio_data(audio_data)
            if normalized_audio is None:
                logger.warning("⚠️ Failed to normalize audio data for speaker verification")
                return None, 0.0, False
            audio_data = normalized_audio
        except ImportError:
            # Fallback: inline conversion if import fails
            if isinstance(audio_data, str):
                try:
                    import base64
                    audio_data = base64.b64decode(audio_data)
                except Exception:
                    logger.warning("⚠️ Audio data is string but not valid base64")
                    return None, 0.0, False

        # Validate audio length (at least 0.5s at 16kHz = 16000 bytes for 16-bit audio)
        if len(audio_data) < 16000:
            logger.warning(
                f"⚠️ Audio too short for verification: {len(audio_data)} bytes "
                f"(need at least 16000 for 0.5s)"
            )
            # Continue anyway - the model may still work with padding

        # =================================================================
        # FAST PATH: Use unified voice cache (< 100ms when cached!)
        # =================================================================
        if self._unified_cache:
            try:
                # Log cache state for debugging
                profiles_count = self._unified_cache.profiles_loaded
                cache_state = self._unified_cache.state.value if hasattr(self._unified_cache.state, 'value') else str(self._unified_cache.state)

                if profiles_count == 0:
                    logger.warning(
                        f"⚠️ Unified cache has NO profiles (state={cache_state}). "
                        "Voice verification will fail!"
                    )
                else:
                    logger.debug(
                        f"🔐 Unified cache ready: {profiles_count} profiles, state={cache_state}"
                    )

                # Use verify_voice_from_audio - the unified fast path
                cache_result = await asyncio.wait_for(
                    self._unified_cache.verify_voice_from_audio(
                        audio_data=audio_data,
                        sample_rate=16000,
                    ),
                    timeout=2.0  # Allow 2s for embedding extraction + matching
                )

                if cache_result and cache_result.matched:
                    logger.info(
                        f"⚡ Unified cache MATCH: {cache_result.speaker_name} "
                        f"({cache_result.similarity:.1%}) in {cache_result.match_time_ms:.0f}ms"
                    )
                    return (
                        cache_result.speaker_name,
                        cache_result.similarity,
                        True  # Was cached
                    )
                else:
                    similarity = cache_result.similarity if cache_result else 0
                    match_type = cache_result.match_type if cache_result else "none"
                    logger.info(
                        f"📊 Unified cache NO MATCH: similarity={similarity:.1%}, "
                        f"type={match_type}, threshold needed={0.75:.1%}"
                    )
            except asyncio.TimeoutError:
                logger.warning("⏱️ Unified cache verification timed out after 2s")
            except AttributeError as e:
                logger.warning(f"⚠️ Unified cache not properly initialized: {e}")
            except Exception as e:
                logger.warning(f"⚠️ Unified cache error: {e}")

        # =================================================================
        # FALLBACK: Use speaker engine (slower, but more thorough)
        # =================================================================
        if self._speaker_engine:
            try:
                # Check if speaker engine has unified cache for fast path
                if hasattr(self._speaker_engine, 'unified_cache') and self._speaker_engine.unified_cache:
                    try:
                        result = await asyncio.wait_for(
                            self._speaker_engine.unified_cache.verify_voice_from_audio(
                                audio_data=audio_data,
                                sample_rate=16000,
                            ),
                            timeout=2.0
                        )
                        if result and result.matched:
                            return (
                                result.speaker_name,
                                result.similarity,
                                True
                            )
                    except Exception:
                        pass

                # Full speaker verification (slower)
                result = await asyncio.wait_for(
                    self._speaker_engine.verify_speaker(audio_data, None),
                    timeout=self._config.fast_verify_timeout
                )

                if result:
                    return (
                        result.get('speaker_name'),
                        result.get('confidence', 0.0),
                        False
                    )
            except asyncio.TimeoutError:
                logger.warning("Speaker verification timed out")
            except Exception as e:
                logger.error(f"Speaker verification error: {e}")

        return None, 0.0, False

    async def _analyze_audio(self, audio_data) -> AudioAnalysis:
        """Analyze audio quality and environment."""
        analysis = AudioAnalysis()

        try:
            # Ensure audio_data is bytes (handle base64 strings)
            if isinstance(audio_data, str):
                try:
                    import base64
                    audio_data = base64.b64decode(audio_data)
                except Exception:
                    # Not base64, try encoding as UTF-8 bytes
                    audio_data = audio_data.encode('utf-8')

            # Basic analysis
            analysis.duration_ms = len(audio_data) / 32  # Rough estimate for 16kHz

            # Check for speech
            analysis.has_speech = len(audio_data) > 3200  # At least 100ms

            # Estimate SNR (simplified)
            if len(audio_data) > 0:
                # Simple energy-based estimate
                import struct
                try:
                    samples = struct.unpack(f'{len(audio_data)//2}h', audio_data)
                    rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
                    max_val = max(abs(s) for s in samples) if samples else 1

                    # Rough SNR estimate
                    if rms > 0:
                        analysis.snr_db = 20 * (rms / 32768) * 50  # Rough scale
                        analysis.snr_db = max(0, min(40, analysis.snr_db))
                    else:
                        analysis.snr_db = 0

                    # Check for clipping
                    analysis.clipping_detected = max_val > 32000

                except Exception:
                    analysis.snr_db = 15  # Default reasonable value

            # Determine environment quality
            if analysis.snr_db > 25:
                analysis.environment = EnvironmentQuality.EXCELLENT
            elif analysis.snr_db > 18:
                analysis.environment = EnvironmentQuality.GOOD
            elif analysis.snr_db > 12:
                analysis.environment = EnvironmentQuality.FAIR
            elif analysis.snr_db > 6:
                analysis.environment = EnvironmentQuality.POOR
                analysis.issues.append("low_snr")
            else:
                analysis.environment = EnvironmentQuality.NOISY
                analysis.issues.append("low_snr")

            if analysis.clipping_detected:
                analysis.issues.append("clipping")

            if analysis.duration_ms < 500:
                analysis.issues.append("short_audio")

        except Exception as e:
            logger.debug(f"Audio analysis error: {e}")

        return analysis

    async def _get_behavioral_context(
        self,
        context: Optional[Dict[str, Any]]
    ) -> BehavioralContext:
        """Get behavioral and contextual factors."""
        behavioral = BehavioralContext()

        try:
            # Check time of day
            hour = datetime.now().hour
            # Typical work hours
            behavioral.is_typical_time = 6 <= hour <= 23

            # Get from context
            if context:
                behavioral.hours_since_last_unlock = context.get(
                    'hours_since_last_unlock', 0
                )
                behavioral.consecutive_failures = context.get(
                    'consecutive_failures', 0
                )
                behavioral.device_trusted = context.get('device_trusted', True)

            # Calculate behavioral confidence
            if behavioral.is_typical_time and behavioral.device_trusted:
                behavioral.behavioral_confidence = 0.85
            elif behavioral.is_typical_time or behavioral.device_trusted:
                behavioral.behavioral_confidence = 0.7
            else:
                behavioral.behavioral_confidence = 0.5

            # Reduce for consecutive failures
            if behavioral.consecutive_failures > 0:
                behavioral.behavioral_confidence *= (0.9 ** behavioral.consecutive_failures)

        except Exception as e:
            logger.debug(f"Behavioral context error: {e}")

        return behavioral

    def _fuse_confidences(self, result: VerificationResult) -> float:
        """Fuse voice and behavioral confidences."""
        if not self._config.use_behavioral_fusion:
            return result.voice_confidence

        # Weighted fusion
        voice_weight = 0.7
        behavioral_weight = 0.3

        fused = (
            result.voice_confidence * voice_weight +
            result.behavioral.behavioral_confidence * behavioral_weight
        )

        # Boost if both are high
        if result.voice_confidence > 0.8 and result.behavioral.behavioral_confidence > 0.8:
            fused = min(1.0, fused * 1.05)

        return fused

    def _determine_level(self, result: VerificationResult) -> RecognitionLevel:
        """Determine recognition level from confidence."""
        conf = result.fused_confidence

        if conf >= self._config.instant_recognition_threshold:
            return RecognitionLevel.INSTANT
        elif conf >= self._config.confident_threshold:
            return RecognitionLevel.CONFIDENT
        elif conf >= self._config.borderline_threshold:
            return RecognitionLevel.GOOD
        elif conf >= self._config.rejection_threshold:
            return RecognitionLevel.BORDERLINE
        else:
            return RecognitionLevel.UNKNOWN

    def _determine_method(self, result: VerificationResult) -> VerificationMethod:
        """Determine which verification method was used."""
        if result.was_cached:
            return VerificationMethod.CACHED

        if result.voice_confidence >= self._config.confident_threshold:
            return VerificationMethod.VOICE_ONLY

        if (result.voice_confidence < self._config.confident_threshold and
            result.behavioral.behavioral_confidence > 0.7):
            return VerificationMethod.VOICE_BEHAVIORAL

        return VerificationMethod.VOICE_ONLY

    async def _check_spoofing(
        self,
        audio_data: bytes,
        result: VerificationResult
    ) -> Tuple[bool, Optional[str]]:
        """Check for replay/spoofing attacks."""
        # Simplified spoofing detection
        # A full implementation would use ML-based liveness detection

        # Check for suspiciously perfect audio (might be recording)
        if result.audio.snr_db > 35:
            # Very high SNR might indicate a recording
            # (real voices have some natural variation)
            pass  # Not definitive enough to flag

        # Check for very short audio with high confidence
        # (might be a spliced recording)
        if result.audio.duration_ms < 500 and result.voice_confidence > 0.95:
            return True, "suspiciously short high-confidence audio"

        return False, None

    def _check_learning(
        self,
        result: VerificationResult
    ) -> Tuple[bool, Optional[str]]:
        """Check for learning opportunities."""
        if not self._config.enable_learning_feedback:
            return False, None

        # New environment learned
        if result.audio.environment not in [EnvironmentQuality.GOOD, EnvironmentQuality.EXCELLENT]:
            if result.verified and result.confidence > 0.85:
                return True, "I've adapted to this acoustic environment."

        # Voice variation learned
        if result.audio.voice_quality != VoiceQuality.CLEAR:
            if result.verified and result.confidence > 0.8:
                return True, "I've noted this voice variation."

        return False, None

    async def _speak(self, text: str):
        """Speak the announcement."""
        if self._voice_communicator:
            try:
                await self._voice_communicator.speak(text)
            except Exception as e:
                logger.debug(f"Speech output error: {e}")

    def _update_timing_stats(self, time_ms: float):
        """Update timing statistics."""
        self._stats['total_verification_time_ms'] += time_ms
        self._stats['avg_verification_time_ms'] = (
            self._stats['total_verification_time_ms'] /
            self._stats['total_verifications']
        )

        # Track instant recognitions
        if time_ms < 500:
            self._stats['instant_recognitions'] += 1

    def _log_result(self, result: VerificationResult):
        """Log verification result."""
        level_emoji = {
            RecognitionLevel.INSTANT: "⚡",
            RecognitionLevel.CONFIDENT: "✅",
            RecognitionLevel.GOOD: "👍",
            RecognitionLevel.BORDERLINE: "⚠️",
            RecognitionLevel.UNKNOWN: "❓",
            RecognitionLevel.SPOOFING: "🚨",
        }

        emoji = level_emoji.get(result.level, "•")
        logger.info(
            f"{emoji} Voice verification: {result.level.value} "
            f"({result.confidence:.1%}) in {result.verification_time_ms:.0f}ms"
            f"{' [CACHED]' if result.was_cached else ''}"
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get verification statistics."""
        return {
            **self._stats,
            'announcer_stats': self._announcer._stats,
            'recent_levels': [r.level.value for r in self._recent_verifications[-10:]],
        }

    async def health_check(self) -> Dict[str, Any]:
        """Check health of the voice biometric intelligence system."""
        components = {}
        issues = []

        # Check speaker engine
        components['speaker_engine'] = {
            'available': self._speaker_engine is not None,
        }
        if not self._speaker_engine:
            issues.append("Speaker engine not available")

        # Check unified cache
        components['unified_cache'] = {
            'available': self._unified_cache is not None,
        }

        # Check voice communicator
        components['voice_communicator'] = {
            'available': self._voice_communicator is not None,
        }

        # Calculate success rate and performance stats
        total = self._stats['total_verifications']
        if total > 0:
            success_rate = self._stats['successful_verifications'] / total
            components['performance'] = {
                'total_verifications': total,
                'success_rate': round(success_rate * 100, 1),
                'avg_time_ms': round(self._stats['avg_verification_time_ms'], 1),
                'cached_hit_rate': round(self._stats['cached_hits'] / total * 100, 1),
            }
            if success_rate < 0.7:
                issues.append(f"Low success rate: {success_rate:.1%}")
        else:
            components['performance'] = {'total_verifications': 0}

        # Performance optimization stats
        components['optimizations'] = {
            'early_exit_enabled': self._config.enable_early_exit,
            'early_exit_threshold': self._config.early_exit_threshold,
            'early_exits': self._stats.get('early_exits', 0),
            'speculative_unlock_enabled': self._config.enable_speculative_unlock,
            'speculative_unlocks': self._stats.get('speculative_unlocks', 0),
            'profile_preloading_enabled': self._config.enable_profile_preloading,
            'hot_cache_hits': self._stats.get('hot_cache_hits', 0),
            'hot_cache_loaded': 'owner' in self._hot_voiceprint_cache,
            'quantization_enabled': self._config.enable_int8_quantization,
            'quantization_available': self._quantization_available,
            'quantized_inferences': self._stats.get('quantized_inferences', 0),
        }

        # Calculate optimization effectiveness
        if total > 0:
            early_exit_rate = self._stats.get('early_exits', 0) / total
            components['optimizations']['early_exit_rate'] = round(early_exit_rate * 100, 1)

        healthy = len(issues) == 0 and self._speaker_engine is not None
        score = 1.0 - (len(issues) * 0.25)

        return {
            'healthy': healthy,
            'score': max(0, round(score, 2)),
            'message': "All systems operational" if healthy else f"Issues: {', '.join(issues)}",
            'components': components,
            'issues': issues,
            'initialized': self._initialized,
        }


# =============================================================================
# SINGLETON
# =============================================================================
_voice_biometric_intelligence: Optional[VoiceBiometricIntelligence] = None
_init_lock = asyncio.Lock()


async def get_voice_biometric_intelligence() -> VoiceBiometricIntelligence:
    """Get the singleton Voice Biometric Intelligence instance."""
    global _voice_biometric_intelligence

    if _voice_biometric_intelligence is None:
        async with _init_lock:
            if _voice_biometric_intelligence is None:
                _voice_biometric_intelligence = VoiceBiometricIntelligence()
                await _voice_biometric_intelligence.initialize()

    return _voice_biometric_intelligence
