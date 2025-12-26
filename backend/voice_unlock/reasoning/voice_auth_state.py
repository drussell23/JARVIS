"""
Voice Authentication Reasoning State Models v2.0

Enterprise-grade Pydantic models for LangGraph voice authentication reasoning.
Features:
- Fully environment-variable driven (zero hardcoding)
- Async-compatible state management
- Rich type hints and validation
- Comprehensive audit trail support
- Multi-factor evidence tracking
- Hypothesis management with Bayesian updates
- Performance metrics and timing
- Integration hooks for observability

Author: JARVIS AI System
"""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import cached_property
from typing import (
    Any,
    Callable,
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
from uuid import uuid4

import numpy as np
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    computed_field,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Environment-Driven Configuration (Zero Hardcoding)
# =============================================================================

class VoiceAuthConfig:
    """
    Dynamic configuration from environment variables.

    All values can be overridden at runtime via environment variables.
    Provides sensible defaults while allowing full customization.
    """

    # -------------------------------------------------------------------------
    # Confidence Thresholds
    # -------------------------------------------------------------------------
    @staticmethod
    def get_instant_threshold() -> float:
        """Threshold for instant recognition (skip reasoning)."""
        return float(os.getenv('VBI_INSTANT_THRESHOLD', '0.92'))

    @staticmethod
    def get_confident_threshold() -> float:
        """Threshold for high confidence authentication."""
        return float(os.getenv('VBI_CONFIDENT_THRESHOLD', '0.85'))

    @staticmethod
    def get_borderline_threshold() -> float:
        """Threshold below which reasoning is triggered."""
        return float(os.getenv('VBI_BORDERLINE_THRESHOLD', '0.75'))

    @staticmethod
    def get_rejection_threshold() -> float:
        """Threshold below which authentication is rejected."""
        return float(os.getenv('VBI_REJECTION_THRESHOLD', '0.60'))

    @staticmethod
    def get_spoofing_threshold() -> float:
        """Threshold for spoofing detection."""
        return float(os.getenv('VBI_SPOOFING_THRESHOLD', '0.70'))

    # -------------------------------------------------------------------------
    # Reasoning Configuration
    # -------------------------------------------------------------------------
    @staticmethod
    def is_reasoning_enabled() -> bool:
        """Whether LangGraph reasoning is enabled."""
        return os.getenv('VOICE_AUTH_REASONING_ENABLED', 'true').lower() == 'true'

    @staticmethod
    def get_reasoning_max_depth() -> int:
        """Maximum reasoning chain depth."""
        return int(os.getenv('VOICE_AUTH_REASONING_MAX_DEPTH', '5'))

    @staticmethod
    def get_reasoning_min_confidence() -> float:
        """Minimum confidence for reasoning decisions."""
        return float(os.getenv('VOICE_AUTH_REASONING_MIN_CONFIDENCE', '0.6'))

    @staticmethod
    def get_reasoning_strategy() -> str:
        """Default reasoning strategy (adaptive, linear, tree, debate)."""
        return os.getenv('VOICE_AUTH_REASONING_STRATEGY', 'adaptive')

    # -------------------------------------------------------------------------
    # Hypothesis Configuration
    # -------------------------------------------------------------------------
    @staticmethod
    def get_max_hypotheses() -> int:
        """Maximum hypotheses to generate."""
        return int(os.getenv('VOICE_AUTH_MAX_HYPOTHESES', '5'))

    @staticmethod
    def get_hypothesis_noise_threshold() -> float:
        """SNR threshold for noise hypothesis (dB)."""
        return float(os.getenv('VOICE_AUTH_HYPOTHESIS_NOISE_THRESHOLD', '15.0'))

    @staticmethod
    def get_hypothesis_behavioral_threshold() -> float:
        """Behavioral confidence threshold for hypothesis generation."""
        return float(os.getenv('VOICE_AUTH_HYPOTHESIS_BEHAVIORAL_THRESHOLD', '0.8'))

    @staticmethod
    def get_hypothesis_prior_update_rate() -> float:
        """Rate for Bayesian prior updates."""
        return float(os.getenv('VOICE_AUTH_HYPOTHESIS_PRIOR_UPDATE_RATE', '0.1'))

    # -------------------------------------------------------------------------
    # Retry & Recovery Configuration
    # -------------------------------------------------------------------------
    @staticmethod
    def get_max_retry_attempts() -> int:
        """Maximum retry attempts before fallback."""
        return int(os.getenv('VOICE_AUTH_MAX_RETRY_ATTEMPTS', '3'))

    @staticmethod
    def get_retry_delay_ms() -> int:
        """Delay between retries in milliseconds."""
        return int(os.getenv('VOICE_AUTH_RETRY_DELAY_MS', '500'))

    @staticmethod
    def get_recovery_boost() -> float:
        """Confidence boost for recovery scenarios."""
        return float(os.getenv('VOICE_AUTH_RECOVERY_BOOST', '0.15'))

    @staticmethod
    def get_max_recovery_confidence() -> float:
        """Maximum confidence after recovery boost."""
        return float(os.getenv('VOICE_AUTH_MAX_RECOVERY_CONFIDENCE', '0.80'))

    # -------------------------------------------------------------------------
    # Timeout Configuration (milliseconds)
    # -------------------------------------------------------------------------
    @staticmethod
    def get_perception_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_PERCEPTION_TIMEOUT_MS', '100'))

    @staticmethod
    def get_analysis_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_ANALYSIS_TIMEOUT_MS', '200'))

    @staticmethod
    def get_verification_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_VERIFICATION_TIMEOUT_MS', '1000'))

    @staticmethod
    def get_evidence_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_EVIDENCE_TIMEOUT_MS', '500'))

    @staticmethod
    def get_hypothesis_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_HYPOTHESIS_TIMEOUT_MS', '200'))

    @staticmethod
    def get_reasoning_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_REASONING_TIMEOUT_MS', '300'))

    @staticmethod
    def get_decision_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_DECISION_TIMEOUT_MS', '100'))

    @staticmethod
    def get_total_timeout_ms() -> float:
        return float(os.getenv('VOICE_AUTH_TOTAL_TIMEOUT_MS', '5000'))

    # -------------------------------------------------------------------------
    # Bayesian Fusion Weights
    # -------------------------------------------------------------------------
    @staticmethod
    def get_ml_weight() -> float:
        return float(os.getenv('BAYESIAN_ML_WEIGHT', '0.40'))

    @staticmethod
    def get_physics_weight() -> float:
        return float(os.getenv('BAYESIAN_PHYSICS_WEIGHT', '0.30'))

    @staticmethod
    def get_behavioral_weight() -> float:
        return float(os.getenv('BAYESIAN_BEHAVIORAL_WEIGHT', '0.20'))

    @staticmethod
    def get_context_weight() -> float:
        return float(os.getenv('BAYESIAN_CONTEXT_WEIGHT', '0.10'))

    @staticmethod
    def get_prior_authentic() -> float:
        return float(os.getenv('BAYESIAN_PRIOR_AUTHENTIC', '0.85'))

    # -------------------------------------------------------------------------
    # Audio Analysis Thresholds
    # -------------------------------------------------------------------------
    @staticmethod
    def get_min_snr_db() -> float:
        """Minimum acceptable SNR in dB."""
        return float(os.getenv('VOICE_AUTH_MIN_SNR_DB', '10.0'))

    @staticmethod
    def get_excellent_snr_db() -> float:
        """SNR threshold for excellent quality."""
        return float(os.getenv('VOICE_AUTH_EXCELLENT_SNR_DB', '25.0'))

    @staticmethod
    def get_min_speech_ratio() -> float:
        """Minimum speech-to-silence ratio."""
        return float(os.getenv('VOICE_AUTH_MIN_SPEECH_RATIO', '0.3'))

    @staticmethod
    def get_min_audio_duration_ms() -> float:
        """Minimum audio duration in milliseconds."""
        return float(os.getenv('VOICE_AUTH_MIN_AUDIO_DURATION_MS', '500'))

    # -------------------------------------------------------------------------
    # Feature Flags
    # -------------------------------------------------------------------------
    @staticmethod
    def is_early_exit_enabled() -> bool:
        """Enable fast path for high confidence."""
        return os.getenv('VBI_ENABLE_EARLY_EXIT', 'true').lower() == 'true'

    @staticmethod
    def is_speculative_unlock_enabled() -> bool:
        """Enable speculative unlock preparation."""
        return os.getenv('VBI_SPECULATIVE_UNLOCK', 'true').lower() == 'true'

    @staticmethod
    def is_physics_spoofing_enabled() -> bool:
        """Enable physics-aware anti-spoofing."""
        return os.getenv('VBI_PHYSICS_SPOOFING', 'true').lower() == 'true'

    @staticmethod
    def is_bayesian_fusion_enabled() -> bool:
        """Enable Bayesian multi-factor fusion."""
        return os.getenv('VBI_BAYESIAN_FUSION', 'true').lower() == 'true'

    @staticmethod
    def is_learning_enabled() -> bool:
        """Enable continuous learning from outcomes."""
        return os.getenv('VOICE_AUTH_LEARNING_ENABLED', 'true').lower() == 'true'

    @staticmethod
    def is_checkpointing_enabled() -> bool:
        """Enable LangGraph checkpointing for recovery."""
        return os.getenv('VOICE_AUTH_CHECKPOINTING_ENABLED', 'true').lower() == 'true'


# =============================================================================
# Enums with Rich Metadata
# =============================================================================

class VoiceAuthReasoningPhase(str, Enum):
    """
    Phases in the voice authentication reasoning pipeline.

    Each phase has a target duration and can transition to specific next phases.
    """
    PERCEIVING = "perceiving"
    ANALYZING = "analyzing"
    VERIFYING = "verifying"
    COLLECTING_EVIDENCE = "collecting_evidence"
    HYPOTHESIZING = "hypothesizing"
    REASONING = "reasoning"
    DECIDING = "deciding"
    RESPONDING = "responding"
    LEARNING = "learning"
    COMPLETED = "completed"
    ERROR_RECOVERY = "error_recovery"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    @property
    def target_duration_ms(self) -> float:
        """Target duration for this phase."""
        durations = {
            self.PERCEIVING: VoiceAuthConfig.get_perception_timeout_ms(),
            self.ANALYZING: VoiceAuthConfig.get_analysis_timeout_ms(),
            self.VERIFYING: VoiceAuthConfig.get_verification_timeout_ms(),
            self.COLLECTING_EVIDENCE: VoiceAuthConfig.get_evidence_timeout_ms(),
            self.HYPOTHESIZING: VoiceAuthConfig.get_hypothesis_timeout_ms(),
            self.REASONING: VoiceAuthConfig.get_reasoning_timeout_ms(),
            self.DECIDING: VoiceAuthConfig.get_decision_timeout_ms(),
            self.RESPONDING: 50.0,
            self.LEARNING: 100.0,
        }
        return durations.get(self, 100.0)

    @property
    def can_skip(self) -> bool:
        """Whether this phase can be skipped in fast path."""
        skippable = {
            self.HYPOTHESIZING,
            self.REASONING,
            self.LEARNING,
        }
        return self in skippable

    def next_phases(self) -> List['VoiceAuthReasoningPhase']:
        """Valid next phases from this phase."""
        transitions = {
            self.PERCEIVING: [self.ANALYZING, self.ERROR_RECOVERY],
            self.ANALYZING: [self.VERIFYING, self.ERROR_RECOVERY],
            self.VERIFYING: [self.COLLECTING_EVIDENCE, self.DECIDING, self.ERROR_RECOVERY],
            self.COLLECTING_EVIDENCE: [self.HYPOTHESIZING, self.DECIDING],
            self.HYPOTHESIZING: [self.REASONING, self.DECIDING],
            self.REASONING: [self.DECIDING],
            self.DECIDING: [self.RESPONDING],
            self.RESPONDING: [self.LEARNING, self.COMPLETED],
            self.LEARNING: [self.COMPLETED],
            self.ERROR_RECOVERY: [self.COMPLETED, self.PERCEIVING],
        }
        return transitions.get(self, [self.COMPLETED])


class ConfidenceLevel(str, Enum):
    """Authentication confidence levels with thresholds."""
    INSTANT = "instant"          # Immediate recognition
    CONFIDENT = "confident"      # High confidence
    GOOD = "good"               # Acceptable
    BORDERLINE = "borderline"    # Needs reasoning
    LOW = "low"                 # Likely fail
    UNKNOWN = "unknown"          # Cannot determine

    @classmethod
    def from_confidence(cls, confidence: float) -> 'ConfidenceLevel':
        """Determine level from confidence score."""
        if confidence >= VoiceAuthConfig.get_instant_threshold():
            return cls.INSTANT
        elif confidence >= VoiceAuthConfig.get_confident_threshold():
            return cls.CONFIDENT
        elif confidence >= VoiceAuthConfig.get_borderline_threshold():
            return cls.GOOD
        elif confidence >= VoiceAuthConfig.get_rejection_threshold():
            return cls.BORDERLINE
        elif confidence > 0:
            return cls.LOW
        else:
            return cls.UNKNOWN

    @property
    def requires_reasoning(self) -> bool:
        """Whether this level requires deep reasoning."""
        return self in {self.BORDERLINE, self.LOW}

    @property
    def can_authenticate(self) -> bool:
        """Whether this level can result in authentication."""
        return self in {self.INSTANT, self.CONFIDENT, self.GOOD}


class DecisionType(str, Enum):
    """Authentication decision types with action mapping."""
    AUTHENTICATE = "authenticate"
    REJECT = "reject"
    CHALLENGE = "challenge"
    ESCALATE = "escalate"
    RETRY = "retry"
    TIMEOUT = "timeout"

    @property
    def is_final(self) -> bool:
        """Whether this is a final decision."""
        return self in {self.AUTHENTICATE, self.REJECT, self.ESCALATE}

    @property
    def unlocks_screen(self) -> bool:
        """Whether this decision unlocks the screen."""
        return self == self.AUTHENTICATE


class HypothesisCategory(str, Enum):
    """Categories for borderline case hypotheses."""
    DIFFERENT_MICROPHONE = "different_microphone"
    SICK_VOICE = "sick_voice"
    BACKGROUND_NOISE = "background_noise"
    TIRED_VOICE = "tired_voice"
    STRESSED_VOICE = "stressed_voice"
    DIFFERENT_ENVIRONMENT = "different_environment"
    VOICE_AGING = "voice_aging"
    REPLAY_ATTACK = "replay_attack"
    SYNTHETIC_VOICE = "synthetic_voice"
    VOICE_CONVERSION = "voice_conversion"
    UNKNOWN_SPEAKER = "unknown_speaker"
    AUDIO_QUALITY = "audio_quality"
    EQUIPMENT_FAILURE = "equipment_failure"

    @property
    def is_security_threat(self) -> bool:
        """Whether this hypothesis indicates a security threat."""
        return self in {
            self.REPLAY_ATTACK,
            self.SYNTHETIC_VOICE,
            self.VOICE_CONVERSION,
            self.UNKNOWN_SPEAKER,
        }

    @property
    def default_prior(self) -> float:
        """Default prior probability for this hypothesis."""
        priors = {
            self.DIFFERENT_MICROPHONE: 0.4,
            self.SICK_VOICE: 0.3,
            self.BACKGROUND_NOISE: 0.5,
            self.TIRED_VOICE: 0.35,
            self.STRESSED_VOICE: 0.25,
            self.DIFFERENT_ENVIRONMENT: 0.3,
            self.VOICE_AGING: 0.1,
            self.REPLAY_ATTACK: 0.1,
            self.SYNTHETIC_VOICE: 0.05,
            self.VOICE_CONVERSION: 0.05,
            self.UNKNOWN_SPEAKER: 0.15,
            self.AUDIO_QUALITY: 0.4,
            self.EQUIPMENT_FAILURE: 0.1,
        }
        return priors.get(self, 0.3)

    @property
    def suggested_action(self) -> str:
        """Default suggested action for this hypothesis."""
        actions = {
            self.DIFFERENT_MICROPHONE: "ask_about_microphone",
            self.SICK_VOICE: "acknowledge_and_verify_pattern",
            self.BACKGROUND_NOISE: "suggest_quiet_environment",
            self.TIRED_VOICE: "acknowledge_and_proceed_carefully",
            self.STRESSED_VOICE: "acknowledge_and_proceed_carefully",
            self.DIFFERENT_ENVIRONMENT: "learn_new_environment",
            self.VOICE_AGING: "update_baseline",
            self.REPLAY_ATTACK: "deny_and_log",
            self.SYNTHETIC_VOICE: "deny_and_alert",
            self.VOICE_CONVERSION: "deny_and_alert",
            self.UNKNOWN_SPEAKER: "deny_access",
            self.AUDIO_QUALITY: "retry_with_guidance",
            self.EQUIPMENT_FAILURE: "suggest_alternative_auth",
        }
        return actions.get(self, "retry")


class EnvironmentQuality(str, Enum):
    """Audio environment quality levels."""
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    NOISY = "noisy"
    VERY_NOISY = "very_noisy"

    @classmethod
    def from_snr(cls, snr_db: float) -> 'EnvironmentQuality':
        """Determine quality from SNR."""
        if snr_db >= VoiceAuthConfig.get_excellent_snr_db():
            return cls.EXCELLENT
        elif snr_db >= 20:
            return cls.GOOD
        elif snr_db >= 15:
            return cls.FAIR
        elif snr_db >= 10:
            return cls.POOR
        elif snr_db >= 5:
            return cls.NOISY
        else:
            return cls.VERY_NOISY

    @property
    def confidence_adjustment(self) -> float:
        """Confidence adjustment for this environment quality."""
        adjustments = {
            self.EXCELLENT: 0.02,
            self.GOOD: 0.0,
            self.FAIR: -0.02,
            self.POOR: -0.05,
            self.NOISY: -0.08,
            self.VERY_NOISY: -0.12,
        }
        return adjustments.get(self, 0.0)


class VoiceQuality(str, Enum):
    """Voice quality indicators."""
    CLEAR = "clear"
    MUFFLED = "muffled"
    HOARSE = "hoarse"
    TIRED = "tired"
    STRESSED = "stressed"
    WHISPER = "whisper"
    SHAKY = "shaky"
    DISTORTED = "distorted"

    @property
    def likely_hypothesis(self) -> Optional[HypothesisCategory]:
        """Most likely hypothesis for this voice quality."""
        mapping = {
            self.MUFFLED: HypothesisCategory.DIFFERENT_MICROPHONE,
            self.HOARSE: HypothesisCategory.SICK_VOICE,
            self.TIRED: HypothesisCategory.TIRED_VOICE,
            self.STRESSED: HypothesisCategory.STRESSED_VOICE,
            self.DISTORTED: HypothesisCategory.AUDIO_QUALITY,
        }
        return mapping.get(self)


class ThoughtType(str, Enum):
    """Types of reasoning thoughts."""
    OBSERVATION = "observation"
    ANALYSIS = "analysis"
    HYPOTHESIS = "hypothesis"
    INFERENCE = "inference"
    EVALUATION = "evaluation"
    DECISION = "decision"
    REFLECTION = "reflection"
    CORRECTION = "correction"
    PREDICTION = "prediction"
    VERIFICATION = "verification"


# =============================================================================
# Data Classes for Intermediate Results
# =============================================================================

@dataclass
class AudioAnalysisResult:
    """
    Comprehensive audio quality analysis results.

    Captures all relevant audio characteristics for authentication decisions.
    """
    snr_db: float = 0.0
    environment_quality: EnvironmentQuality = EnvironmentQuality.GOOD
    voice_quality: VoiceQuality = VoiceQuality.CLEAR
    has_speech: bool = True
    speech_ratio: float = 0.0
    duration_ms: float = 0.0
    sample_rate: int = 16000
    channels: int = 1
    bit_depth: int = 16
    detected_issues: List[str] = field(default_factory=list)
    clipping_detected: bool = False
    clipping_ratio: float = 0.0
    silence_ratio: float = 0.0
    peak_amplitude: float = 0.0
    rms_amplitude: float = 0.0
    spectral_centroid_hz: float = 0.0
    spectral_bandwidth_hz: float = 0.0
    zero_crossing_rate: float = 0.0
    fundamental_frequency_hz: float = 0.0
    formant_frequencies: List[float] = field(default_factory=list)
    estimated_room_reverb_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snr_db": self.snr_db,
            "environment_quality": self.environment_quality.value,
            "voice_quality": self.voice_quality.value,
            "has_speech": self.has_speech,
            "speech_ratio": self.speech_ratio,
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "detected_issues": self.detected_issues,
            "clipping_detected": self.clipping_detected,
            "fundamental_frequency_hz": self.fundamental_frequency_hz,
            "estimated_room_reverb_ms": self.estimated_room_reverb_ms,
        }

    @property
    def is_acceptable(self) -> bool:
        """Whether audio quality is acceptable for authentication."""
        return (
            self.has_speech
            and self.snr_db >= VoiceAuthConfig.get_min_snr_db()
            and self.speech_ratio >= VoiceAuthConfig.get_min_speech_ratio()
            and self.duration_ms >= VoiceAuthConfig.get_min_audio_duration_ms()
            and not self.clipping_detected
        )

    @property
    def quality_score(self) -> float:
        """Overall quality score (0-1)."""
        score = 1.0

        # SNR contribution
        snr_factor = min(1.0, self.snr_db / VoiceAuthConfig.get_excellent_snr_db())
        score *= (0.3 + 0.7 * snr_factor)

        # Speech ratio contribution
        score *= (0.5 + 0.5 * self.speech_ratio)

        # Issues penalty
        score *= max(0.5, 1.0 - 0.1 * len(self.detected_issues))

        # Clipping penalty
        if self.clipping_detected:
            score *= 0.8

        return round(score, 3)


@dataclass
class BehavioralContext:
    """
    Behavioral analysis context for multi-factor authentication.

    Captures time, location, device, and pattern information.
    """
    # Time patterns
    is_typical_time: bool = True
    hour_of_day: int = 0
    day_of_week: int = 0
    is_weekend: bool = False
    is_holiday: bool = False
    hours_since_last_unlock: float = 0.0
    unlocks_today: int = 0
    unlocks_this_week: int = 0

    # Location patterns
    is_typical_location: bool = True
    wifi_network_hash: str = ""
    wifi_network_known: bool = True
    location_type: str = "unknown"  # home, office, cafe, outdoor

    # Device patterns
    is_typical_device: bool = True
    device_trusted: bool = True
    microphone_type: str = "unknown"
    bluetooth_devices: List[str] = field(default_factory=list)
    apple_watch_connected: bool = False
    apple_watch_authenticated: bool = False

    # Failure patterns
    consecutive_failures: int = 0
    recent_failed_attempts: int = 0
    last_failure_reason: Optional[str] = None

    # Computed confidence
    behavioral_confidence: float = 0.0
    pattern_match_score: float = 0.0
    anomaly_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_typical_time": self.is_typical_time,
            "is_typical_location": self.is_typical_location,
            "is_typical_device": self.is_typical_device,
            "hours_since_last_unlock": self.hours_since_last_unlock,
            "consecutive_failures": self.consecutive_failures,
            "behavioral_confidence": self.behavioral_confidence,
            "anomaly_score": self.anomaly_score,
            "apple_watch_connected": self.apple_watch_connected,
        }

    def compute_confidence(self) -> float:
        """Compute behavioral confidence from factors."""
        score = 1.0

        # Time factor
        if not self.is_typical_time:
            score *= 0.85

        # Location factor
        if not self.is_typical_location:
            score *= 0.80
        elif not self.wifi_network_known:
            score *= 0.90

        # Device factor
        if not self.is_typical_device:
            score *= 0.85

        # Failure penalty
        if self.consecutive_failures > 0:
            score *= max(0.6, 1.0 - 0.1 * self.consecutive_failures)

        # Apple Watch bonus
        if self.apple_watch_authenticated:
            score = min(1.0, score * 1.05)

        self.behavioral_confidence = round(score, 3)
        return self.behavioral_confidence


@dataclass
class PhysicsAnalysis:
    """
    Physics-aware verification results.

    Includes VTL, liveness, spoofing detection, and acoustic analysis.
    """
    # Overall confidence
    physics_confidence: float = 0.0

    # Vocal Tract Length analysis
    vtl_verified: bool = True
    vtl_estimated_cm: float = 0.0
    vtl_baseline_cm: float = 0.0
    vtl_deviation_cm: float = 0.0
    vtl_within_tolerance: bool = True

    # Liveness detection
    liveness_passed: bool = True
    liveness_confidence: float = 0.0
    breathing_detected: bool = True
    micro_variations_present: bool = True
    natural_pauses: bool = True

    # Spoofing detection
    spoofing_detected: bool = False
    spoofing_type: Optional[str] = None
    spoofing_confidence: float = 0.0
    replay_score: float = 0.0
    synthetic_score: float = 0.0
    deepfake_score: float = 0.0

    # Acoustic analysis
    double_reverb_detected: bool = False
    double_reverb_confidence: float = 0.0
    doppler_pattern: str = "natural"  # natural, static, erratic
    room_impulse_consistent: bool = True

    # Audio fingerprint (for replay detection)
    audio_fingerprint_hash: str = ""
    fingerprint_match_found: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "physics_confidence": self.physics_confidence,
            "vtl_verified": self.vtl_verified,
            "vtl_deviation_cm": self.vtl_deviation_cm,
            "liveness_passed": self.liveness_passed,
            "liveness_confidence": self.liveness_confidence,
            "spoofing_detected": self.spoofing_detected,
            "spoofing_type": self.spoofing_type,
            "spoofing_confidence": self.spoofing_confidence,
            "replay_score": self.replay_score,
            "synthetic_score": self.synthetic_score,
            "deepfake_score": self.deepfake_score,
        }

    @property
    def is_authentic(self) -> bool:
        """Whether the audio appears to be authentic."""
        return (
            not self.spoofing_detected
            and self.liveness_passed
            and self.vtl_verified
            and not self.double_reverb_detected
            and not self.fingerprint_match_found
        )

    def compute_confidence(self) -> float:
        """Compute physics confidence from factors."""
        if self.spoofing_detected:
            return 0.0

        score = 1.0

        # VTL factor
        if not self.vtl_verified:
            score *= 0.7
        elif self.vtl_deviation_cm > 1.0:
            score *= max(0.8, 1.0 - 0.1 * self.vtl_deviation_cm)

        # Liveness factor
        score *= (0.3 + 0.7 * self.liveness_confidence)

        # Spoofing scores (invert - high score is bad)
        score *= (1.0 - 0.3 * self.replay_score)
        score *= (1.0 - 0.4 * self.synthetic_score)
        score *= (1.0 - 0.5 * self.deepfake_score)

        # Double reverb penalty
        if self.double_reverb_detected:
            score *= 0.6

        self.physics_confidence = round(max(0.0, score), 3)
        return self.physics_confidence


# =============================================================================
# Hypothesis Model
# =============================================================================

class VoiceAuthHypothesis(BaseModel):
    """
    A hypothesis about why authentication confidence is borderline.

    Supports Bayesian probability updates and evidence tracking.
    """
    hypothesis_id: str = Field(default_factory=lambda: str(uuid4()))
    category: HypothesisCategory
    description: str = ""

    # Evidence tracking
    evidence_for: List[str] = Field(default_factory=list)
    evidence_against: List[str] = Field(default_factory=list)
    evidence_weight: float = 0.0

    # Bayesian probabilities
    prior_probability: float = Field(default=0.5)
    posterior_probability: float = Field(default=0.5)
    likelihood_ratio: float = Field(default=1.0)

    # Action
    suggested_action: str = ""
    confidence: float = 0.5

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    evaluated: bool = False
    evaluation_notes: str = ""

    class Config:
        use_enum_values = True

    def model_post_init(self, __context: Any) -> None:
        """Set defaults from category if not provided."""
        if not self.prior_probability:
            self.prior_probability = self.category.default_prior if isinstance(self.category, HypothesisCategory) else 0.5
        if not self.suggested_action:
            cat = HypothesisCategory(self.category) if isinstance(self.category, str) else self.category
            self.suggested_action = cat.suggested_action

    def update_posterior(self, likelihood_true: float, likelihood_false: float) -> float:
        """
        Update posterior probability using Bayes' theorem.

        P(H|E) = P(E|H) * P(H) / P(E)
        where P(E) = P(E|H) * P(H) + P(E|~H) * P(~H)
        """
        prior = self.prior_probability

        # Compute marginal likelihood P(E)
        p_evidence = (likelihood_true * prior) + (likelihood_false * (1 - prior))

        if p_evidence > 0:
            self.posterior_probability = (likelihood_true * prior) / p_evidence
            self.likelihood_ratio = likelihood_true / max(0.001, likelihood_false)

        self.evaluated = True
        return self.posterior_probability

    def add_evidence(self, evidence: str, supports: bool, weight: float = 1.0) -> None:
        """Add evidence for or against this hypothesis."""
        if supports:
            self.evidence_for.append(evidence)
            self.evidence_weight += weight
        else:
            self.evidence_against.append(evidence)
            self.evidence_weight -= weight

    @property
    def net_evidence_score(self) -> float:
        """Net evidence score (positive = supporting, negative = contradicting)."""
        return len(self.evidence_for) - len(self.evidence_against)

    @property
    def is_security_threat(self) -> bool:
        """Whether this hypothesis indicates a security threat."""
        cat = HypothesisCategory(self.category) if isinstance(self.category, str) else self.category
        return cat.is_security_threat


# =============================================================================
# Reasoning Thought Model
# =============================================================================

class ReasoningThought(BaseModel):
    """
    A single thought in the reasoning chain.

    Supports chain-of-thought transparency and audit trails.
    """
    thought_id: str = Field(default_factory=lambda: str(uuid4()))
    thought_type: ThoughtType
    content: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    # Evidence and reasoning
    evidence: List[str] = Field(default_factory=list)
    reasoning: str = ""

    # Dependencies
    parent_thought_ids: List[str] = Field(default_factory=list)
    child_thought_ids: List[str] = Field(default_factory=list)

    # Metadata
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    phase: str = ""
    duration_ms: float = 0.0

    class Config:
        use_enum_values = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thought_id": self.thought_id,
            "thought_type": self.thought_type,
            "content": self.content,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp.isoformat(),
        }


# =============================================================================
# Main State Model
# =============================================================================

class VoiceAuthReasoningState(BaseModel):
    """
    State for voice authentication reasoning graph.

    Comprehensive state model that flows through all LangGraph nodes,
    accumulating analysis results, hypotheses, and decisions.

    Features:
    - All thresholds from VoiceAuthConfig (environment-driven)
    - Rich type hints and validation
    - Computed properties for decision support
    - Full audit trail support
    - Serialization for checkpointing
    """

    # =========================================================================
    # Identity & Session
    # =========================================================================
    reasoning_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(default="")
    trace_id: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)

    # =========================================================================
    # Phase Control
    # =========================================================================
    phase: VoiceAuthReasoningPhase = Field(default=VoiceAuthReasoningPhase.PERCEIVING)
    previous_phase: Optional[VoiceAuthReasoningPhase] = Field(default=None)
    phase_history: List[str] = Field(default_factory=list)

    # =========================================================================
    # Input Data
    # =========================================================================
    audio_data: Optional[bytes] = Field(default=None, exclude=True)
    audio_hash: str = Field(default="")
    audio_fingerprint: str = Field(default="")
    context: Dict[str, Any] = Field(default_factory=dict)
    input_source: str = Field(default="microphone")  # microphone, file, stream

    # =========================================================================
    # Audio Analysis Results
    # =========================================================================
    audio_duration_ms: float = Field(default=0.0)
    snr_db: float = Field(default=0.0)
    environment_quality: EnvironmentQuality = Field(default=EnvironmentQuality.GOOD)
    voice_quality: VoiceQuality = Field(default=VoiceQuality.CLEAR)
    has_speech: bool = Field(default=True)
    speech_ratio: float = Field(default=0.0)
    detected_issues: List[str] = Field(default_factory=list)
    audio_quality_score: float = Field(default=0.0)
    fundamental_frequency_hz: float = Field(default=0.0)
    formant_frequencies: List[float] = Field(default_factory=list)

    # =========================================================================
    # ML Verification Results
    # =========================================================================
    ml_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    speaker_name: Optional[str] = Field(default=None)
    speaker_verified: bool = Field(default=False)
    was_cached: bool = Field(default=False)
    cache_hit_type: Optional[str] = Field(default=None)  # hot, warm, cold
    embedding_extracted: bool = Field(default=False)
    voice_embedding: Optional[List[float]] = Field(default=None, exclude=True)
    embedding_dimension: int = Field(default=192)
    similarity_score: float = Field(default=0.0)

    # =========================================================================
    # Physics Analysis Results
    # =========================================================================
    physics_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    vtl_verified: bool = Field(default=True)
    vtl_estimated_cm: float = Field(default=0.0)
    vtl_deviation_cm: float = Field(default=0.0)
    liveness_passed: bool = Field(default=True)
    liveness_confidence: float = Field(default=0.0)
    spoofing_detected: bool = Field(default=False)
    spoofing_type: Optional[str] = Field(default=None)
    spoofing_confidence: float = Field(default=0.0)
    replay_score: float = Field(default=0.0)
    synthetic_score: float = Field(default=0.0)
    deepfake_score: float = Field(default=0.0)

    # =========================================================================
    # Behavioral Context Results
    # =========================================================================
    behavioral_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_typical_time: bool = Field(default=True)
    is_typical_location: bool = Field(default=True)
    is_typical_device: bool = Field(default=True)
    hours_since_last_unlock: float = Field(default=0.0)
    consecutive_failures: int = Field(default=0, ge=0)
    anomaly_score: float = Field(default=0.0)
    apple_watch_connected: bool = Field(default=False)
    apple_watch_authenticated: bool = Field(default=False)

    # =========================================================================
    # Context Confidence
    # =========================================================================
    context_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # =========================================================================
    # Visual Security Results (v6.2 NEW)
    # =========================================================================
    visual_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_threat_detected: bool = Field(default=False)
    visual_security_status: Optional[str] = Field(default=None)
    visual_should_proceed: bool = Field(default=True)
    visual_warning_message: str = Field(default="")
    visual_evidence: Optional[Any] = Field(default=None, exclude=True)

    # =========================================================================
    # Hypotheses (for borderline cases)
    # =========================================================================
    hypotheses: List[VoiceAuthHypothesis] = Field(default_factory=list)
    active_hypothesis_id: Optional[str] = Field(default=None)
    hypotheses_evaluated: int = Field(default=0)
    best_hypothesis_category: Optional[str] = Field(default=None)
    best_hypothesis_probability: float = Field(default=0.0)

    # =========================================================================
    # Reasoning Chain (Chain of Thought)
    # =========================================================================
    thoughts: List[ReasoningThought] = Field(default_factory=list)
    reasoning_trace: str = Field(default="")
    reasoning_depth: int = Field(default=0)
    reasoning_strategy: str = Field(default_factory=VoiceAuthConfig.get_reasoning_strategy)
    reasoning_branches: int = Field(default=0)

    # =========================================================================
    # Fusion & Decision
    # =========================================================================
    fused_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    bayesian_authentic_prob: float = Field(default=0.0, ge=0.0, le=1.0)
    bayesian_spoof_prob: float = Field(default=0.0, ge=0.0, le=1.0)
    dominant_factor: str = Field(default="")
    confidence_level: ConfidenceLevel = Field(default=ConfidenceLevel.UNKNOWN)
    decision: DecisionType = Field(default=DecisionType.AUTHENTICATE)
    decision_reasoning: str = Field(default="")
    decision_factors: Dict[str, float] = Field(default_factory=dict)

    # =========================================================================
    # Response Generation
    # =========================================================================
    announcement: str = Field(default="")
    announcement_tone: str = Field(default="neutral")  # confident, cautious, apologetic
    retry_guidance: Optional[str] = Field(default=None)
    learned_something: bool = Field(default=False)
    learning_note: Optional[str] = Field(default=None)
    security_alert: bool = Field(default=False)
    security_alert_message: Optional[str] = Field(default=None)

    # =========================================================================
    # Control Flow
    # =========================================================================
    attempt_number: int = Field(default=1, ge=1)
    max_attempts: int = Field(default_factory=VoiceAuthConfig.get_max_retry_attempts)
    should_retry: bool = Field(default=False)
    retry_strategy: Optional[str] = Field(default=None)
    retry_filters_applied: List[str] = Field(default_factory=list)
    iterations: int = Field(default=0)
    max_iterations: int = Field(default=5)
    early_exit_triggered: bool = Field(default=False)
    fast_path_used: bool = Field(default=False)

    # =========================================================================
    # Timing & Performance
    # =========================================================================
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    phase_timings: Dict[str, float] = Field(default_factory=dict)
    total_time_ms: float = Field(default=0.0)
    timeout_triggered: bool = Field(default=False)

    # =========================================================================
    # Thresholds (from environment config - computed at instantiation)
    # =========================================================================
    instant_threshold: float = Field(default_factory=VoiceAuthConfig.get_instant_threshold)
    confident_threshold: float = Field(default_factory=VoiceAuthConfig.get_confident_threshold)
    borderline_threshold: float = Field(default_factory=VoiceAuthConfig.get_borderline_threshold)
    rejection_threshold: float = Field(default_factory=VoiceAuthConfig.get_rejection_threshold)

    # =========================================================================
    # Observability
    # =========================================================================
    span_ids: Dict[str, str] = Field(default_factory=dict)
    cost_metrics: Dict[str, float] = Field(default_factory=dict)
    total_cost: float = Field(default=0.0)
    cache_savings: float = Field(default=0.0)

    # =========================================================================
    # Error Handling
    # =========================================================================
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    recovery_attempted: bool = Field(default=False)
    recovery_successful: bool = Field(default=False)

    class Config:
        arbitrary_types_allowed = True
        use_enum_values = True

    # =========================================================================
    # Validators
    # =========================================================================
    @field_validator('audio_hash', mode='before')
    @classmethod
    def compute_audio_hash(cls, v: str, info) -> str:
        """Compute audio hash if not provided."""
        if v:
            return v
        audio_data = info.data.get('audio_data')
        if audio_data:
            return hashlib.sha256(audio_data).hexdigest()[:16]
        return ""

    @model_validator(mode='after')
    def set_defaults(self) -> 'VoiceAuthReasoningState':
        """Set computed defaults after initialization."""
        if self.started_at is None:
            self.started_at = datetime.utcnow()
        return self

    # =========================================================================
    # Computed Properties
    # =========================================================================
    @computed_field
    @property
    def is_high_confidence(self) -> bool:
        """Whether we have high enough confidence for fast path."""
        return self.ml_confidence >= self.instant_threshold

    @computed_field
    @property
    def needs_reasoning(self) -> bool:
        """Whether deep reasoning is needed for this case."""
        if not VoiceAuthConfig.is_reasoning_enabled():
            return False
        return (
            self.borderline_threshold <= self.ml_confidence < self.confident_threshold
            or (self.ml_confidence < self.borderline_threshold and self.behavioral_confidence > 0.8)
        )

    @computed_field
    @property
    def is_spoofing_risk(self) -> bool:
        """Whether there's a spoofing risk detected."""
        return (
            self.spoofing_detected
            or self.replay_score > VoiceAuthConfig.get_spoofing_threshold()
            or self.synthetic_score > VoiceAuthConfig.get_spoofing_threshold()
            or self.deepfake_score > VoiceAuthConfig.get_spoofing_threshold()
        )

    @computed_field
    @property
    def can_authenticate(self) -> bool:
        """Whether authentication is possible based on current state."""
        return (
            not self.spoofing_detected
            and self.liveness_passed
            and self.has_speech
            and (self.fused_confidence >= self.confident_threshold or
                 self.bayesian_authentic_prob >= self.confident_threshold)
        )

    @computed_field
    @property
    def duration_ms(self) -> float:
        """Total duration of this reasoning session."""
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        elif self.started_at:
            return (datetime.utcnow() - self.started_at).total_seconds() * 1000
        return 0.0

    # =========================================================================
    # Methods
    # =========================================================================
    def transition_to(self, new_phase: VoiceAuthReasoningPhase) -> None:
        """Transition to a new phase with validation."""
        if new_phase not in self.phase.next_phases():
            logger.warning(
                f"Unusual phase transition: {self.phase.value} -> {new_phase.value}"
            )

        self.previous_phase = self.phase
        self.phase = new_phase
        self.phase_history.append(new_phase.value)

    def add_thought(
        self,
        thought_type: ThoughtType,
        content: str,
        confidence: float = 0.5,
        evidence: Optional[List[str]] = None,
        reasoning: str = "",
    ) -> ReasoningThought:
        """Add a thought to the reasoning chain."""
        thought = ReasoningThought(
            thought_type=thought_type,
            content=content,
            confidence=confidence,
            evidence=evidence or [],
            reasoning=reasoning,
            phase=self.phase.value,
        )
        self.thoughts.append(thought)
        self.reasoning_depth = len(self.thoughts)
        return thought

    def add_hypothesis(
        self,
        category: HypothesisCategory,
        description: str,
        evidence_for: Optional[List[str]] = None,
        evidence_against: Optional[List[str]] = None,
        prior: Optional[float] = None,
    ) -> VoiceAuthHypothesis:
        """Add a hypothesis for borderline case analysis."""
        if len(self.hypotheses) >= VoiceAuthConfig.get_max_hypotheses():
            # Remove lowest probability hypothesis
            self.hypotheses.sort(key=lambda h: h.posterior_probability, reverse=True)
            self.hypotheses = self.hypotheses[:VoiceAuthConfig.get_max_hypotheses() - 1]

        hypothesis = VoiceAuthHypothesis(
            category=category,
            description=description,
            evidence_for=evidence_for or [],
            evidence_against=evidence_against or [],
            prior_probability=prior or category.default_prior,
        )
        self.hypotheses.append(hypothesis)
        return hypothesis

    def get_best_hypothesis(self) -> Optional[VoiceAuthHypothesis]:
        """Get the hypothesis with highest posterior probability."""
        if not self.hypotheses:
            return None
        best = max(self.hypotheses, key=lambda h: h.posterior_probability)
        self.best_hypothesis_category = best.category
        self.best_hypothesis_probability = best.posterior_probability
        return best

    def record_phase_timing(self, phase: str, duration_ms: float) -> None:
        """Record timing for a phase."""
        self.phase_timings[phase] = duration_ms
        self.total_time_ms = sum(self.phase_timings.values())

    def add_error(self, error_type: str, message: str, phase: Optional[str] = None) -> None:
        """Record an error."""
        self.errors.append({
            "type": error_type,
            "message": message,
            "phase": phase or self.phase.value,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def add_warning(self, message: str) -> None:
        """Record a warning."""
        self.warnings.append(message)

    def add_cost(self, operation: str, cost: float) -> None:
        """Record cost for an operation."""
        self.cost_metrics[operation] = cost
        self.total_cost = sum(self.cost_metrics.values())

    def compute_confidence_level(self) -> ConfidenceLevel:
        """Determine confidence level from fused/ML confidence."""
        conf = self.fused_confidence or self.ml_confidence
        self.confidence_level = ConfidenceLevel.from_confidence(conf)
        return self.confidence_level

    def to_result_dict(self) -> Dict[str, Any]:
        """Convert to result dictionary for external use."""
        return {
            "reasoning_id": self.reasoning_id,
            "session_id": self.session_id,
            "speaker_name": self.speaker_name,
            "speaker_verified": self.speaker_verified,
            "decision": self.decision,
            "decision_reasoning": self.decision_reasoning,
            "confidence": {
                "ml": self.ml_confidence,
                "physics": self.physics_confidence,
                "behavioral": self.behavioral_confidence,
                "context": self.context_confidence,
                "fused": self.fused_confidence,
                "bayesian_authentic": self.bayesian_authentic_prob,
            },
            "confidence_level": self.confidence_level,
            "dominant_factor": self.dominant_factor,
            "announcement": self.announcement,
            "retry_guidance": self.retry_guidance,
            "learned_something": self.learned_something,
            "learning_note": self.learning_note,
            "hypotheses_count": len(self.hypotheses),
            "best_hypothesis": {
                "category": self.best_hypothesis_category,
                "probability": self.best_hypothesis_probability,
            } if self.best_hypothesis_category else None,
            "reasoning_depth": self.reasoning_depth,
            "total_time_ms": self.total_time_ms,
            "phase_timings": self.phase_timings,
            "spoofing_detected": self.spoofing_detected,
            "spoofing_type": self.spoofing_type,
            "security_alert": self.security_alert,
            "errors": self.errors,
            "warnings": self.warnings,
            "cost": {
                "total": self.total_cost,
                "breakdown": self.cost_metrics,
                "cache_savings": self.cache_savings,
            },
            "fast_path_used": self.fast_path_used,
            "early_exit_triggered": self.early_exit_triggered,
        }

    def to_audit_dict(self) -> Dict[str, Any]:
        """Convert to audit dictionary for Langfuse/logging."""
        return {
            **self.to_result_dict(),
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "audio_hash": self.audio_hash,
            "audio_fingerprint": self.audio_fingerprint,
            "phase_history": self.phase_history,
            "thoughts": [t.to_dict() for t in self.thoughts],
            "hypotheses": [h.model_dump() for h in self.hypotheses],
            "input_source": self.input_source,
            "attempt_number": self.attempt_number,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
