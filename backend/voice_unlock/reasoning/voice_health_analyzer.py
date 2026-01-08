"""
Voice Health Analyzer - Detect Illness, Fatigue, and Stress from Voice.
========================================================================

Analyzes voice characteristics to detect health-related changes that might
affect authentication confidence. Enables intelligent handling when user
is sick, tired, or stressed.

Features:
1. Illness detection (hoarseness, congestion, throat issues)
2. Fatigue detection (lower energy, slower speech)
3. Stress detection (tension, pitch variations)
4. Emotional state inference
5. Voice quality degradation tracking
6. Adaptive threshold recommendations

Per CLAUDE.md:
    "Your voice sounds different today, Derek - are you feeling alright?
    For security, I'd normally ask you to try again, but your speech
    patterns and timing match perfectly."

Author: JARVIS Trinity v81.0 - Voice Health Intelligence
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class VoiceHealthIndicator(Enum):
    """Indicators of voice health state."""
    HEALTHY = "healthy"
    HOARSE = "hoarse"
    CONGESTED = "congested"
    FATIGUED = "fatigued"
    STRESSED = "stressed"
    EMOTIONAL = "emotional"
    DIFFERENT_ENVIRONMENT = "different_environment"
    UNKNOWN = "unknown"


class VoiceConfidenceImpact(Enum):
    """How health state impacts authentication confidence."""
    NO_IMPACT = "no_impact"
    MINOR = "minor"           # -5-10% confidence
    MODERATE = "moderate"     # -10-20% confidence
    SIGNIFICANT = "significant"  # -20-30% confidence
    SEVERE = "severe"         # -30%+ confidence


@dataclass
class VoiceHealthResult:
    """Result of voice health analysis."""
    # Overall assessment
    health_state: VoiceHealthIndicator = VoiceHealthIndicator.HEALTHY
    confidence_impact: VoiceConfidenceImpact = VoiceConfidenceImpact.NO_IMPACT
    health_score: float = 1.0  # 0.0 = very unhealthy, 1.0 = perfectly healthy

    # Specific indicators
    hoarseness_score: float = 0.0
    congestion_score: float = 0.0
    fatigue_score: float = 0.0
    stress_score: float = 0.0
    emotion_score: float = 0.0

    # Acoustic features
    fundamental_frequency: float = 0.0  # F0 in Hz
    f0_deviation_from_baseline: float = 0.0  # % deviation
    jitter: float = 0.0  # Voice instability
    shimmer: float = 0.0  # Amplitude variation
    hnr: float = 0.0  # Harmonics-to-Noise Ratio
    speech_rate: float = 0.0  # Words per minute estimate
    energy_level: float = 0.0  # Normalized energy

    # Context
    comparison_to_baseline: str = ""
    recommended_action: str = ""
    user_feedback_suggestion: str = ""

    # Metadata
    analysis_time_ms: float = 0.0
    confidence_in_analysis: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "health_state": self.health_state.value,
            "confidence_impact": self.confidence_impact.value,
            "health_score": self.health_score,
            "indicators": {
                "hoarseness": self.hoarseness_score,
                "congestion": self.congestion_score,
                "fatigue": self.fatigue_score,
                "stress": self.stress_score,
                "emotion": self.emotion_score,
            },
            "acoustic_features": {
                "fundamental_frequency": self.fundamental_frequency,
                "f0_deviation": self.f0_deviation_from_baseline,
                "jitter": self.jitter,
                "shimmer": self.shimmer,
                "hnr": self.hnr,
            },
            "recommendation": self.recommended_action,
            "feedback_suggestion": self.user_feedback_suggestion,
        }


@dataclass
class VoiceBaseline:
    """Baseline voice characteristics for a user."""
    user_id: str
    avg_fundamental_frequency: float = 0.0
    avg_jitter: float = 0.0
    avg_shimmer: float = 0.0
    avg_hnr: float = 0.0
    avg_energy: float = 0.0
    avg_speech_rate: float = 0.0
    sample_count: int = 0
    last_updated: float = 0.0


# =============================================================================
# Voice Health Analyzer
# =============================================================================

class VoiceHealthAnalyzer:
    """
    Analyzes voice for health-related changes.

    Detects illness, fatigue, stress, and other factors that might
    explain reduced authentication confidence without indicating
    a security threat.

    Usage:
        analyzer = VoiceHealthAnalyzer()
        result = await analyzer.analyze(audio_data, user_id="derek")

        if result.health_state == VoiceHealthIndicator.HOARSE:
            print(result.user_feedback_suggestion)
            # "Your voice sounds different today - are you feeling alright?"
    """

    # Thresholds for health detection
    HOARSENESS_THRESHOLD = 0.4
    CONGESTION_THRESHOLD = 0.35
    FATIGUE_THRESHOLD = 0.5
    STRESS_THRESHOLD = 0.45

    # Baseline deviation thresholds
    F0_DEVIATION_WARNING = 15.0  # % deviation from baseline
    F0_DEVIATION_SIGNIFICANT = 25.0
    JITTER_WARNING = 0.02
    SHIMMER_WARNING = 0.08
    HNR_DROP_WARNING = 5.0  # dB below baseline

    def __init__(
        self,
        enable_baseline_comparison: bool = True,
        enable_spectral_analysis: bool = True,
        enable_prosody_analysis: bool = True,
    ):
        """
        Initialize the voice health analyzer.

        Args:
            enable_baseline_comparison: Compare against user's baseline
            enable_spectral_analysis: Analyze spectral characteristics
            enable_prosody_analysis: Analyze speech rhythm and intonation
        """
        self.enable_baseline = enable_baseline_comparison
        self.enable_spectral = enable_spectral_analysis
        self.enable_prosody = enable_prosody_analysis

        # Baseline cache
        self._baselines: Dict[str, VoiceBaseline] = {}
        self._lock = asyncio.Lock()

        logger.info(
            f"[VoiceHealthAnalyzer] Initialized "
            f"(baseline={enable_baseline_comparison}, "
            f"spectral={enable_spectral_analysis})"
        )

    async def analyze(
        self,
        audio_data: bytes,
        user_id: str,
        sample_rate: int = 16000,
        baseline: Optional[VoiceBaseline] = None,
    ) -> VoiceHealthResult:
        """
        Analyze voice for health indicators.

        Args:
            audio_data: Raw audio bytes
            user_id: User ID for baseline comparison
            sample_rate: Audio sample rate
            baseline: Optional pre-loaded baseline

        Returns:
            VoiceHealthResult with health analysis
        """
        start_time = time.perf_counter()
        result = VoiceHealthResult()

        try:
            # Convert audio to numpy array
            audio_array = self._bytes_to_array(audio_data)

            # Extract acoustic features
            features = await self._extract_features(audio_array, sample_rate)
            result.fundamental_frequency = features.get("f0", 0.0)
            result.jitter = features.get("jitter", 0.0)
            result.shimmer = features.get("shimmer", 0.0)
            result.hnr = features.get("hnr", 0.0)
            result.energy_level = features.get("energy", 0.0)

            # Get or create baseline
            if baseline is None and self.enable_baseline:
                baseline = await self._get_baseline(user_id)

            # Compare to baseline
            if baseline and baseline.sample_count > 5:
                deviation = self._calculate_baseline_deviation(features, baseline)
                result.f0_deviation_from_baseline = deviation.get("f0_deviation", 0.0)
                result.comparison_to_baseline = self._generate_comparison_text(deviation)

            # Analyze specific health indicators
            result.hoarseness_score = await self._detect_hoarseness(features, audio_array)
            result.congestion_score = await self._detect_congestion(features, audio_array)
            result.fatigue_score = await self._detect_fatigue(features, audio_array)
            result.stress_score = await self._detect_stress(features, audio_array)
            result.emotion_score = await self._detect_emotion(features, audio_array)

            # Determine primary health state
            result.health_state = self._determine_health_state(result)

            # Calculate confidence impact
            result.confidence_impact = self._calculate_confidence_impact(result)

            # Calculate overall health score
            result.health_score = self._calculate_health_score(result)

            # Generate recommendations
            result.recommended_action = self._generate_recommendation(result)
            result.user_feedback_suggestion = self._generate_user_feedback(result, user_id)

            # Set confidence in our analysis
            result.confidence_in_analysis = min(
                0.9,
                0.5 + (baseline.sample_count / 100) if baseline else 0.5
            )

        except Exception as e:
            logger.warning(f"[VoiceHealthAnalyzer] Analysis error: {e}")
            result.health_state = VoiceHealthIndicator.UNKNOWN
            result.recommended_action = "Unable to analyze voice health"

        result.analysis_time_ms = (time.perf_counter() - start_time) * 1000
        return result

    def _bytes_to_array(self, audio_data: bytes) -> np.ndarray:
        """Convert audio bytes to numpy array."""
        try:
            return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            return np.zeros(16000, dtype=np.float32)

    async def _extract_features(
        self,
        audio: np.ndarray,
        sample_rate: int,
    ) -> Dict[str, float]:
        """Extract acoustic features from audio."""
        features = {}

        try:
            # Fundamental frequency estimation (simplified)
            features["f0"] = self._estimate_f0(audio, sample_rate)

            # Jitter (pitch perturbation)
            features["jitter"] = self._calculate_jitter(audio, sample_rate)

            # Shimmer (amplitude perturbation)
            features["shimmer"] = self._calculate_shimmer(audio)

            # Harmonics-to-Noise Ratio
            features["hnr"] = self._calculate_hnr(audio, sample_rate)

            # Energy level
            features["energy"] = np.sqrt(np.mean(audio ** 2))

            # Speech rate estimate (based on energy peaks)
            features["speech_rate"] = self._estimate_speech_rate(audio, sample_rate)

        except Exception as e:
            logger.debug(f"[VoiceHealthAnalyzer] Feature extraction error: {e}")

        return features

    def _estimate_f0(self, audio: np.ndarray, sample_rate: int) -> float:
        """Estimate fundamental frequency (simplified autocorrelation)."""
        try:
            # Use autocorrelation for F0 estimation
            min_period = int(sample_rate / 500)  # Max F0: 500 Hz
            max_period = int(sample_rate / 50)   # Min F0: 50 Hz

            if len(audio) < max_period * 2:
                return 0.0

            autocorr = np.correlate(audio[:max_period * 2], audio[:max_period * 2], mode='full')
            autocorr = autocorr[len(autocorr) // 2:]

            # Find first peak after minimum period
            if len(autocorr) > max_period:
                peak_indices = np.where(
                    (autocorr[min_period:max_period] > autocorr[min_period - 1:max_period - 1]) &
                    (autocorr[min_period:max_period] > autocorr[min_period + 1:max_period + 1])
                )[0]

                if len(peak_indices) > 0:
                    period = peak_indices[0] + min_period
                    return sample_rate / period

            return 0.0
        except Exception:
            return 0.0

    def _calculate_jitter(self, audio: np.ndarray, sample_rate: int) -> float:
        """Calculate jitter (pitch instability)."""
        try:
            # Simplified jitter estimation
            f0 = self._estimate_f0(audio, sample_rate)
            if f0 <= 0:
                return 0.05  # Default moderate jitter

            # Estimate period-to-period variation
            period = int(sample_rate / f0)
            periods = len(audio) // period

            if periods < 3:
                return 0.05

            # Calculate local period variations
            period_lengths = []
            for i in range(periods - 1):
                segment = audio[i * period:(i + 2) * period]
                if len(segment) >= period * 2:
                    autocorr = np.correlate(segment[:period], segment[period:], mode='valid')
                    period_lengths.append(np.argmax(autocorr))

            if len(period_lengths) > 2:
                jitter = np.std(period_lengths) / np.mean(period_lengths)
                return min(0.2, jitter)

            return 0.05
        except Exception:
            return 0.05

    def _calculate_shimmer(self, audio: np.ndarray) -> float:
        """Calculate shimmer (amplitude variation)."""
        try:
            # Calculate amplitude envelope
            frame_size = 160  # 10ms at 16kHz
            hop_size = 80

            amplitudes = []
            for i in range(0, len(audio) - frame_size, hop_size):
                frame = audio[i:i + frame_size]
                amplitudes.append(np.max(np.abs(frame)))

            if len(amplitudes) < 3:
                return 0.1

            amplitudes = np.array(amplitudes)
            shimmer = np.mean(np.abs(np.diff(amplitudes))) / np.mean(amplitudes)
            return min(0.3, shimmer)
        except Exception:
            return 0.1

    def _calculate_hnr(self, audio: np.ndarray, sample_rate: int) -> float:
        """Calculate Harmonics-to-Noise Ratio."""
        try:
            f0 = self._estimate_f0(audio, sample_rate)
            if f0 <= 0:
                return 10.0  # Default moderate HNR

            # Simplified HNR estimation using autocorrelation
            period = int(sample_rate / f0)
            if period <= 0 or len(audio) < period * 2:
                return 10.0

            autocorr = np.correlate(audio[:period * 2], audio[:period * 2], mode='full')
            autocorr = autocorr[len(autocorr) // 2:]

            r0 = autocorr[0]  # Energy at lag 0
            r_period = autocorr[period] if len(autocorr) > period else 0

            if r0 > 0 and r_period > 0:
                hnr = 10 * np.log10(r_period / (r0 - r_period + 1e-10))
                return max(-10, min(30, hnr))

            return 10.0
        except Exception:
            return 10.0

    def _estimate_speech_rate(self, audio: np.ndarray, sample_rate: int) -> float:
        """Estimate speech rate based on syllable peaks."""
        try:
            # Calculate energy envelope
            frame_size = int(sample_rate * 0.025)  # 25ms
            hop_size = int(sample_rate * 0.010)    # 10ms

            energy = []
            for i in range(0, len(audio) - frame_size, hop_size):
                frame = audio[i:i + frame_size]
                energy.append(np.sqrt(np.mean(frame ** 2)))

            energy = np.array(energy)
            if len(energy) < 10:
                return 0.0

            # Find peaks (syllables)
            threshold = np.mean(energy) * 0.5
            peaks = 0
            above_threshold = False

            for e in energy:
                if e > threshold and not above_threshold:
                    peaks += 1
                    above_threshold = True
                elif e < threshold:
                    above_threshold = False

            # Convert to syllables per second
            duration_seconds = len(audio) / sample_rate
            return peaks / duration_seconds if duration_seconds > 0 else 0.0

        except Exception:
            return 0.0

    async def _detect_hoarseness(
        self,
        features: Dict[str, float],
        audio: np.ndarray,
    ) -> float:
        """Detect hoarseness in voice."""
        score = 0.0

        # High jitter indicates hoarseness
        jitter = features.get("jitter", 0.0)
        if jitter > self.JITTER_WARNING:
            score += min(0.4, (jitter - self.JITTER_WARNING) * 10)

        # Low HNR indicates hoarseness
        hnr = features.get("hnr", 15.0)
        if hnr < 10:
            score += min(0.3, (10 - hnr) * 0.05)

        # High shimmer indicates hoarseness
        shimmer = features.get("shimmer", 0.0)
        if shimmer > self.SHIMMER_WARNING:
            score += min(0.3, (shimmer - self.SHIMMER_WARNING) * 3)

        return min(1.0, score)

    async def _detect_congestion(
        self,
        features: Dict[str, float],
        audio: np.ndarray,
    ) -> float:
        """Detect nasal congestion in voice."""
        score = 0.0

        # Shifted formants can indicate congestion
        # Using F0 deviation as proxy
        f0 = features.get("f0", 0.0)

        # Lower than expected F0 might indicate congestion
        # (simplified heuristic)
        if f0 < 100 and f0 > 50:  # Male voice range, lower than typical
            score += 0.2

        # Reduced HNR can indicate congested airways
        hnr = features.get("hnr", 15.0)
        if hnr < 12:
            score += 0.2

        return min(1.0, score)

    async def _detect_fatigue(
        self,
        features: Dict[str, float],
        audio: np.ndarray,
    ) -> float:
        """Detect fatigue in voice."""
        score = 0.0

        # Lower energy indicates fatigue
        energy = features.get("energy", 0.0)
        if energy < 0.1:
            score += 0.3

        # Slower speech rate indicates fatigue
        speech_rate = features.get("speech_rate", 0.0)
        if 0 < speech_rate < 3.0:  # Less than 3 syllables/second
            score += 0.2

        # Lower F0 can indicate fatigue
        f0 = features.get("f0", 0.0)
        if f0 < 90:  # Lower than typical
            score += 0.15

        # Higher jitter can indicate fatigue
        jitter = features.get("jitter", 0.0)
        if jitter > 0.015:
            score += 0.15

        return min(1.0, score)

    async def _detect_stress(
        self,
        features: Dict[str, float],
        audio: np.ndarray,
    ) -> float:
        """Detect stress in voice."""
        score = 0.0

        # Higher F0 can indicate stress
        f0 = features.get("f0", 0.0)
        if f0 > 150:  # Higher than typical
            score += 0.25

        # Higher energy can indicate stress
        energy = features.get("energy", 0.0)
        if energy > 0.3:
            score += 0.2

        # Higher jitter under stress
        jitter = features.get("jitter", 0.0)
        if jitter > 0.018:
            score += 0.2

        # Faster speech rate can indicate stress
        speech_rate = features.get("speech_rate", 0.0)
        if speech_rate > 5.0:  # More than 5 syllables/second
            score += 0.15

        return min(1.0, score)

    async def _detect_emotion(
        self,
        features: Dict[str, float],
        audio: np.ndarray,
    ) -> float:
        """Detect emotional state (simplified)."""
        # Combine stress and energy indicators
        stress = await self._detect_stress(features, audio)
        energy = features.get("energy", 0.0)

        # High variability indicates emotional speech
        score = stress * 0.5 + (energy * 0.5 if energy > 0.2 else 0)

        return min(1.0, score)

    def _determine_health_state(self, result: VoiceHealthResult) -> VoiceHealthIndicator:
        """Determine primary health state from indicators."""
        # Find highest scoring indicator
        scores = {
            VoiceHealthIndicator.HOARSE: result.hoarseness_score,
            VoiceHealthIndicator.CONGESTED: result.congestion_score,
            VoiceHealthIndicator.FATIGUED: result.fatigue_score,
            VoiceHealthIndicator.STRESSED: result.stress_score,
            VoiceHealthIndicator.EMOTIONAL: result.emotion_score,
        }

        max_indicator = max(scores.items(), key=lambda x: x[1])

        # Check if any indicator exceeds threshold
        if max_indicator[0] == VoiceHealthIndicator.HOARSE and max_indicator[1] > self.HOARSENESS_THRESHOLD:
            return VoiceHealthIndicator.HOARSE
        elif max_indicator[0] == VoiceHealthIndicator.CONGESTED and max_indicator[1] > self.CONGESTION_THRESHOLD:
            return VoiceHealthIndicator.CONGESTED
        elif max_indicator[0] == VoiceHealthIndicator.FATIGUED and max_indicator[1] > self.FATIGUE_THRESHOLD:
            return VoiceHealthIndicator.FATIGUED
        elif max_indicator[0] == VoiceHealthIndicator.STRESSED and max_indicator[1] > self.STRESS_THRESHOLD:
            return VoiceHealthIndicator.STRESSED
        elif max_indicator[1] > 0.6:  # High emotional
            return VoiceHealthIndicator.EMOTIONAL

        return VoiceHealthIndicator.HEALTHY

    def _calculate_confidence_impact(self, result: VoiceHealthResult) -> VoiceConfidenceImpact:
        """Calculate how health state impacts authentication confidence."""
        if result.health_state == VoiceHealthIndicator.HEALTHY:
            return VoiceConfidenceImpact.NO_IMPACT

        # Calculate combined impact
        max_score = max(
            result.hoarseness_score,
            result.congestion_score,
            result.fatigue_score,
            result.stress_score,
        )

        if max_score > 0.7:
            return VoiceConfidenceImpact.SEVERE
        elif max_score > 0.5:
            return VoiceConfidenceImpact.SIGNIFICANT
        elif max_score > 0.35:
            return VoiceConfidenceImpact.MODERATE
        elif max_score > 0.2:
            return VoiceConfidenceImpact.MINOR

        return VoiceConfidenceImpact.NO_IMPACT

    def _calculate_health_score(self, result: VoiceHealthResult) -> float:
        """Calculate overall health score (0-1)."""
        # Combine all health indicators
        combined = (
            result.hoarseness_score * 0.3 +
            result.congestion_score * 0.2 +
            result.fatigue_score * 0.25 +
            result.stress_score * 0.15 +
            result.emotion_score * 0.1
        )

        return max(0.0, 1.0 - combined)

    def _calculate_baseline_deviation(
        self,
        features: Dict[str, float],
        baseline: VoiceBaseline,
    ) -> Dict[str, float]:
        """Calculate deviation from user's baseline."""
        deviation = {}

        if baseline.avg_fundamental_frequency > 0:
            f0_dev = abs(features.get("f0", 0) - baseline.avg_fundamental_frequency)
            deviation["f0_deviation"] = (f0_dev / baseline.avg_fundamental_frequency) * 100

        if baseline.avg_hnr > 0:
            deviation["hnr_drop"] = baseline.avg_hnr - features.get("hnr", 0)

        if baseline.avg_jitter > 0:
            deviation["jitter_increase"] = features.get("jitter", 0) - baseline.avg_jitter

        return deviation

    def _generate_comparison_text(self, deviation: Dict[str, float]) -> str:
        """Generate comparison to baseline text."""
        f0_dev = deviation.get("f0_deviation", 0)

        if f0_dev > self.F0_DEVIATION_SIGNIFICANT:
            return f"Voice pitch differs by {f0_dev:.0f}% from your usual"
        elif f0_dev > self.F0_DEVIATION_WARNING:
            return f"Voice pitch slightly different ({f0_dev:.0f}% from baseline)"

        return "Voice characteristics within normal range"

    def _generate_recommendation(self, result: VoiceHealthResult) -> str:
        """Generate recommendation based on health state."""
        if result.health_state == VoiceHealthIndicator.HOARSE:
            return "Consider accepting with behavioral verification due to hoarse voice"
        elif result.health_state == VoiceHealthIndicator.CONGESTED:
            return "Nasal congestion detected - use speech pattern matching as backup"
        elif result.health_state == VoiceHealthIndicator.FATIGUED:
            return "User appears fatigued - accept with time-of-day context"
        elif result.health_state == VoiceHealthIndicator.STRESSED:
            return "Stress indicators detected - verify with behavioral patterns"
        elif result.health_state == VoiceHealthIndicator.EMOTIONAL:
            return "Emotional speech detected - consider challenge question"

        return "Proceed with standard authentication"

    def _generate_user_feedback(self, result: VoiceHealthResult, user_id: str) -> str:
        """Generate user-facing feedback per CLAUDE.md guidelines."""
        if result.health_state == VoiceHealthIndicator.HOARSE:
            return (
                f"Your voice sounds different today, {user_id} - are you feeling alright? "
                "For security, I'd normally ask you to try again, but your speech patterns "
                "and timing match perfectly."
            )
        elif result.health_state == VoiceHealthIndicator.CONGESTED:
            return (
                f"I can hear you might have a cold, {user_id}. Your voice sounds different "
                "but I can still verify it's you from your speech patterns."
            )
        elif result.health_state == VoiceHealthIndicator.FATIGUED:
            return (
                f"You sound tired, {user_id}. Everything okay? I've verified it's you "
                "based on your timing and location patterns."
            )
        elif result.health_state == VoiceHealthIndicator.STRESSED:
            return (
                f"I notice some tension in your voice, {user_id}. Is everything alright? "
                "Your identity is confirmed through additional verification."
            )

        return ""

    async def _get_baseline(self, user_id: str) -> Optional[VoiceBaseline]:
        """Get baseline for a user."""
        async with self._lock:
            return self._baselines.get(user_id)

    async def update_baseline(
        self,
        user_id: str,
        features: Dict[str, float],
    ) -> None:
        """Update user's voice baseline with new sample."""
        async with self._lock:
            if user_id not in self._baselines:
                self._baselines[user_id] = VoiceBaseline(user_id=user_id)

            baseline = self._baselines[user_id]
            n = baseline.sample_count

            # Exponential moving average update
            alpha = 0.1 if n > 10 else 0.3

            baseline.avg_fundamental_frequency = (
                (1 - alpha) * baseline.avg_fundamental_frequency +
                alpha * features.get("f0", 0)
            )
            baseline.avg_jitter = (
                (1 - alpha) * baseline.avg_jitter +
                alpha * features.get("jitter", 0)
            )
            baseline.avg_shimmer = (
                (1 - alpha) * baseline.avg_shimmer +
                alpha * features.get("shimmer", 0)
            )
            baseline.avg_hnr = (
                (1 - alpha) * baseline.avg_hnr +
                alpha * features.get("hnr", 0)
            )
            baseline.avg_energy = (
                (1 - alpha) * baseline.avg_energy +
                alpha * features.get("energy", 0)
            )

            baseline.sample_count += 1
            baseline.last_updated = time.time()


# =============================================================================
# Singleton Access
# =============================================================================

_analyzer_instance: Optional[VoiceHealthAnalyzer] = None
_analyzer_lock = asyncio.Lock()


async def get_voice_health_analyzer() -> VoiceHealthAnalyzer:
    """Get the singleton voice health analyzer."""
    global _analyzer_instance

    async with _analyzer_lock:
        if _analyzer_instance is None:
            _analyzer_instance = VoiceHealthAnalyzer()
        return _analyzer_instance


async def analyze_voice_health(
    audio_data: bytes,
    user_id: str,
    sample_rate: int = 16000,
) -> VoiceHealthResult:
    """Convenience function to analyze voice health."""
    analyzer = await get_voice_health_analyzer()
    return await analyzer.analyze(audio_data, user_id, sample_rate)
