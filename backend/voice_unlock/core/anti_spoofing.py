"""
Advanced Anti-Spoofing Detection for Voice Unlock System

Comprehensive detection of:
- Replay attacks (audio fingerprinting + temporal analysis)
- Synthetic voice (TTS detection via spectral/temporal markers)
- Recording playback (speaker/room acoustics detection)
- Voice conversion (formant/pitch manipulation detection)
- Liveness detection (micro-variations, breathing patterns)
- Physics-aware verification (VTL, reverb, Doppler) [v2.0]

Physics-Aware Integration v2.0:
- Vocal Tract Length (VTL) verification
- Reverberation/double-reverb detection
- Doppler effect analysis for liveness
- Bayesian confidence fusion
"""

import logging
import hashlib
import os
import numpy as np
from typing import Tuple, Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from collections import deque
import asyncio

# Import physics-aware components
from .feature_extraction import (
    PhysicsAwareFeatureExtractor,
    PhysicsAwareFeatures,
    PhysicsConfidenceLevel,
    get_physics_feature_extractor
)

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - Environment-driven
# =============================================================================

class AntiSpoofingConfig:
    """Anti-spoofing configuration from environment."""
    # Physics integration
    PHYSICS_ENABLED = os.getenv("ANTISPOOFING_PHYSICS_ENABLED", "true").lower() == "true"
    PHYSICS_WEIGHT = float(os.getenv("ANTISPOOFING_PHYSICS_WEIGHT", "0.35"))

    # Detection thresholds
    REPLAY_THRESHOLD = float(os.getenv("REPLAY_DETECTION_THRESHOLD", "0.8"))
    SYNTHETIC_THRESHOLD = float(os.getenv("SYNTHETIC_DETECTION_THRESHOLD", "0.6"))
    RECORDING_THRESHOLD = float(os.getenv("RECORDING_DETECTION_THRESHOLD", "0.5"))

    # Physics thresholds
    VTL_DEVIATION_THRESHOLD_CM = float(os.getenv("VTL_DEVIATION_THRESHOLD", "2.0"))
    DOUBLE_REVERB_THRESHOLD = float(os.getenv("DOUBLE_REVERB_THRESHOLD", "0.7"))

    # Risk scoring
    RISK_WEIGHT_REPLAY = float(os.getenv("RISK_WEIGHT_REPLAY", "0.25"))
    RISK_WEIGHT_SYNTHETIC = float(os.getenv("RISK_WEIGHT_SYNTHETIC", "0.20"))
    RISK_WEIGHT_RECORDING = float(os.getenv("RISK_WEIGHT_RECORDING", "0.15"))
    RISK_WEIGHT_PHYSICS = float(os.getenv("RISK_WEIGHT_PHYSICS", "0.25"))
    RISK_WEIGHT_LIVENESS = float(os.getenv("RISK_WEIGHT_LIVENESS", "0.15"))


class SpoofType(str, Enum):
    """Types of detected spoofing attacks."""
    NONE = "none"
    REPLAY_ATTACK = "replay_attack"
    SYNTHETIC_VOICE = "synthetic_voice"
    RECORDING_PLAYBACK = "recording_playback"
    VOICE_CONVERSION = "voice_conversion"
    TEXT_TO_SPEECH = "text_to_speech"
    DEEPFAKE = "deepfake"
    LIVENESS_FAILED = "liveness_failed"
    # Physics-aware detection types (v2.0)
    PHYSICS_VIOLATION = "physics_violation"
    DOUBLE_REVERB = "double_reverb"
    VTL_MISMATCH = "vtl_mismatch"
    UNNATURAL_MOVEMENT = "unnatural_movement"


@dataclass
class SpoofingResult:
    """Result of anti-spoofing analysis."""
    is_spoofed: bool
    spoof_type: SpoofType
    confidence: float
    details: Dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"  # low, medium, high, critical
    recommendations: List[str] = field(default_factory=list)
    # Physics-aware extensions (v2.0)
    physics_analysis: Optional[PhysicsAwareFeatures] = None
    physics_confidence: float = 0.0
    bayesian_authentic_probability: float = 0.0


@dataclass
class AudioCharacteristics:
    """Extracted audio characteristics for analysis."""
    sample_rate: int = 16000
    duration_ms: float = 0.0
    rms_energy: float = 0.0
    zero_crossing_rate: float = 0.0
    spectral_centroid: float = 0.0
    spectral_bandwidth: float = 0.0
    spectral_rolloff: float = 0.0
    mfcc_features: Optional[np.ndarray] = None
    pitch_mean: float = 0.0
    pitch_variance: float = 0.0
    silence_ratio: float = 0.0
    snr_estimate: float = 0.0


class AntiSpoofingDetector:
    """
    Advanced voice spoofing detection system.

    Detection layers:
    1. Replay Attack Detection - Audio fingerprint + temporal matching
    2. Synthetic Voice Detection - Spectral analysis for TTS artifacts
    3. Recording Playback Detection - Room acoustics + speaker detection
    4. Voice Conversion Detection - Formant/pitch manipulation analysis
    5. Liveness Detection - Micro-variations + breathing patterns
    6. Deepfake Detection - Temporal inconsistencies + artifact analysis
    7. Physics-Aware Detection - VTL, reverb, Doppler analysis [v2.0]

    Physics-Aware Integration:
    - Vocal tract length verification (biometric uniqueness)
    - Double-reverb detection (replay attack indicator)
    - Doppler effect analysis (natural movement liveness)
    - Bayesian confidence fusion (multi-factor decision)
    """

    def __init__(
        self,
        fingerprint_cache_ttl: int = 3600,
        min_audio_duration_ms: float = 500.0,
        sample_rate: int = 16000,
        enable_learning: bool = True,
        enable_physics: bool = True
    ):
        self.fingerprint_cache_ttl = fingerprint_cache_ttl
        self.min_audio_duration_ms = min_audio_duration_ms
        self.sample_rate = sample_rate
        self.enable_learning = enable_learning
        self.enable_physics = enable_physics and AntiSpoofingConfig.PHYSICS_ENABLED
        self.config = AntiSpoofingConfig()

        # Fingerprint cache for replay detection
        self._audio_fingerprints: Dict[str, datetime] = {}
        self._cleanup_threshold = 100

        # Short-term similarity cache (for detecting quick replay attempts)
        self._recent_embeddings: deque = deque(maxlen=20)

        # Learned baseline characteristics for the enrolled user
        self._user_baseline: Optional[AudioCharacteristics] = None
        self._baseline_samples: List[AudioCharacteristics] = []

        # Statistics for adaptive thresholds
        self._detection_history: List[Dict[str, Any]] = []

        # Physics-aware extractor (v2.0)
        self._physics_extractor: Optional[PhysicsAwareFeatureExtractor] = None
        if self.enable_physics:
            try:
                self._physics_extractor = get_physics_feature_extractor(sample_rate)
                logger.info("✅ Physics-aware detection enabled")
            except Exception as e:
                logger.warning(f"⚠️ Physics-aware detection unavailable: {e}")
                self.enable_physics = False

        # Physics baseline
        self._baseline_vtl: Optional[float] = None
        self._baseline_rt60: Optional[float] = None

        logger.info(f"✅ AntiSpoofingDetector initialized with {'7' if self.enable_physics else '6'} detection layers")

    async def detect_spoofing_async(
        self,
        audio_data: bytes,
        speaker_name: str = "unknown",
        context: Optional[Dict[str, Any]] = None,
        ml_confidence: Optional[float] = None,
        behavioral_confidence: Optional[float] = None
    ) -> SpoofingResult:
        """
        Async version of spoofing detection with physics-aware analysis.

        Args:
            audio_data: Raw audio bytes (16-bit PCM)
            speaker_name: Expected speaker name
            context: Optional context (time, location, device info)
            ml_confidence: ML embedding confidence for Bayesian fusion
            behavioral_confidence: Behavioral pattern confidence
        """
        # Run physics analysis in parallel with traditional detection
        physics_task = None
        if self.enable_physics and self._physics_extractor:
            physics_task = self._physics_extractor.extract_physics_features_async(
                audio_data,
                ml_confidence=ml_confidence,
                behavioral_confidence=behavioral_confidence,
                context_confidence=context.get("context_confidence") if context else None
            )

        # Run traditional detection
        loop = asyncio.get_event_loop()
        traditional_task = loop.run_in_executor(
            None,
            self._detect_spoofing_traditional,
            audio_data,
            speaker_name,
            context
        )

        # Await results
        if physics_task:
            physics_result, traditional_result = await asyncio.gather(
                physics_task, traditional_task
            )
            # Combine physics and traditional results
            return self._combine_physics_and_traditional(
                traditional_result, physics_result, ml_confidence
            )
        else:
            return await traditional_task

    def _combine_physics_and_traditional(
        self,
        traditional: SpoofingResult,
        physics: PhysicsAwareFeatures,
        ml_confidence: Optional[float]
    ) -> SpoofingResult:
        """Combine physics-aware and traditional anti-spoofing results."""

        # If traditional already detected spoofing with high confidence, use it
        if traditional.is_spoofed and traditional.confidence > 0.9:
            traditional.physics_analysis = physics
            traditional.physics_confidence = physics.physics_confidence
            traditional.bayesian_authentic_probability = physics.bayesian_authentic_probability
            return traditional

        # Physics-based spoofing detection
        physics_spoofed = False
        physics_spoof_type = SpoofType.NONE
        physics_confidence = 0.0

        # Check for double-reverb (strong replay indicator)
        if physics.reverb_analysis.double_reverb_detected:
            if physics.reverb_analysis.double_reverb_confidence > self.config.DOUBLE_REVERB_THRESHOLD:
                physics_spoofed = True
                physics_spoof_type = SpoofType.DOUBLE_REVERB
                physics_confidence = physics.reverb_analysis.double_reverb_confidence
                traditional.recommendations.append(
                    f"Double-reverb detected (confidence: {physics_confidence:.2f}) - replay attack suspected"
                )

        # Check VTL consistency
        if not physics_spoofed and physics.vocal_tract.vtl_estimated_cm > 0:
            if not physics.vocal_tract.is_within_human_range:
                physics_spoofed = True
                physics_spoof_type = SpoofType.VTL_MISMATCH
                physics_confidence = 0.9
                traditional.recommendations.append(
                    f"VTL outside human range ({physics.vocal_tract.vtl_estimated_cm:.1f} cm) - voice conversion suspected"
                )
            elif not physics.vocal_tract.is_consistent_with_baseline:
                if physics.vocal_tract.vtl_deviation_cm > self.config.VTL_DEVIATION_THRESHOLD_CM:
                    physics_spoofed = True
                    physics_spoof_type = SpoofType.VTL_MISMATCH
                    physics_confidence = min(0.9, physics.vocal_tract.vtl_deviation_cm / 3.0)
                    traditional.recommendations.append(
                        f"VTL deviation ({physics.vocal_tract.vtl_deviation_cm:.1f} cm) exceeds threshold"
                    )

        # Check for unnatural movement (possible recording)
        if not physics_spoofed and not physics.doppler.is_natural_movement:
            if physics.doppler.movement_pattern == "none":
                # Static audio - could be recording
                physics_confidence = max(physics_confidence, 0.5)
                traditional.recommendations.append(
                    "Static audio detected - possible recording playback"
                )
            elif physics.doppler.movement_pattern == "erratic":
                physics_spoofed = True
                physics_spoof_type = SpoofType.UNNATURAL_MOVEMENT
                physics_confidence = 0.7
                traditional.recommendations.append(
                    "Erratic frequency patterns - possible manipulation"
                )

        # Combine results
        if physics_spoofed and not traditional.is_spoofed:
            # Physics detected spoof that traditional missed
            return SpoofingResult(
                is_spoofed=True,
                spoof_type=physics_spoof_type,
                confidence=physics_confidence,
                details={
                    **traditional.details,
                    "physics_detection": {
                        "vtl_cm": physics.vocal_tract.vtl_estimated_cm,
                        "double_reverb": physics.reverb_analysis.double_reverb_detected,
                        "movement_pattern": physics.doppler.movement_pattern,
                        "physics_level": physics.physics_level.value,
                        "anomalies": physics.anomalies_detected
                    }
                },
                risk_level="high" if physics_confidence > 0.7 else "medium",
                recommendations=traditional.recommendations,
                physics_analysis=physics,
                physics_confidence=physics.physics_confidence,
                bayesian_authentic_probability=physics.bayesian_authentic_probability
            )

        # Adjust traditional result with physics info
        if traditional.is_spoofed:
            # Physics can increase confidence in traditional detection
            if physics.physics_level == PhysicsConfidenceLevel.PHYSICS_FAILED:
                traditional.confidence = min(1.0, traditional.confidence + 0.1)
                traditional.risk_level = "critical"
        else:
            # Physics can provide additional assurance
            if physics.physics_level == PhysicsConfidenceLevel.PHYSICS_VERIFIED:
                traditional.confidence = max(traditional.confidence, physics.physics_confidence)
                traditional.recommendations.append(
                    f"Physics verification passed (VTL: {physics.vocal_tract.vtl_estimated_cm:.1f} cm)"
                )

        # Always include physics analysis
        traditional.physics_analysis = physics
        traditional.physics_confidence = physics.physics_confidence
        traditional.bayesian_authentic_probability = physics.bayesian_authentic_probability

        # Update risk score with physics
        if physics.anomalies_detected:
            traditional.details["physics_anomalies"] = physics.anomalies_detected

        return traditional

    def _detect_spoofing_traditional(
        self,
        audio_data: bytes,
        speaker_name: str = "unknown",
        context: Optional[Dict[str, Any]] = None
    ) -> SpoofingResult:
        """Traditional (non-physics) spoofing detection - layers 1-6."""
        return self.detect_spoofing(audio_data, speaker_name, context)

    def detect_spoofing(
        self,
        audio_data: bytes,
        speaker_name: str = "unknown",
        context: Optional[Dict[str, Any]] = None
    ) -> SpoofingResult:
        """
        Comprehensive multi-layer spoofing detection.

        Args:
            audio_data: Raw audio bytes (16-bit PCM)
            speaker_name: Expected speaker name
            context: Optional context (time, location, device info)

        Returns:
            SpoofingResult with detection outcome and details
        """
        context = context or {}
        all_details = {"checks_performed": []}
        risk_score = 0.0
        recommendations = []

        try:
            # Extract audio characteristics
            characteristics = self._extract_characteristics(audio_data)
            all_details["audio_characteristics"] = {
                "duration_ms": characteristics.duration_ms,
                "rms_energy": characteristics.rms_energy,
                "snr_estimate": characteristics.snr_estimate,
                "silence_ratio": characteristics.silence_ratio
            }

            # Validate minimum audio quality
            if characteristics.duration_ms < self.min_audio_duration_ms:
                return SpoofingResult(
                    is_spoofed=False,
                    spoof_type=SpoofType.NONE,
                    confidence=0.0,
                    details={"error": "audio_too_short", "duration_ms": characteristics.duration_ms},
                    risk_level="unknown",
                    recommendations=["Provide longer audio sample (min 500ms)"]
                )

            # Layer 1: Replay Attack Detection
            all_details["checks_performed"].append("replay_detection")
            is_replay, replay_conf, replay_details = self._detect_replay_attack_advanced(
                audio_data, characteristics
            )
            all_details["replay_analysis"] = replay_details
            if is_replay:
                return SpoofingResult(
                    is_spoofed=True,
                    spoof_type=SpoofType.REPLAY_ATTACK,
                    confidence=replay_conf,
                    details=all_details,
                    risk_level="critical",
                    recommendations=["Replay attack detected - request new live sample"]
                )
            risk_score += (1 - replay_conf) * 0.1

            # Layer 2: Synthetic Voice Detection
            all_details["checks_performed"].append("synthetic_detection")
            is_synthetic, synth_conf, synth_details = self._detect_synthetic_voice_advanced(
                audio_data, characteristics
            )
            all_details["synthetic_analysis"] = synth_details
            if is_synthetic:
                return SpoofingResult(
                    is_spoofed=True,
                    spoof_type=SpoofType.SYNTHETIC_VOICE,
                    confidence=synth_conf,
                    details=all_details,
                    risk_level="critical",
                    recommendations=["Synthetic voice detected - TTS or voice synthesis suspected"]
                )
            risk_score += synth_details.get("synthesis_score", 0.0) * 0.2

            # Layer 3: Recording Playback Detection
            all_details["checks_performed"].append("recording_detection")
            is_recording, rec_conf, rec_details = self._detect_recording_playback_advanced(
                audio_data, characteristics
            )
            all_details["recording_analysis"] = rec_details
            if is_recording:
                return SpoofingResult(
                    is_spoofed=True,
                    spoof_type=SpoofType.RECORDING_PLAYBACK,
                    confidence=rec_conf,
                    details=all_details,
                    risk_level="high",
                    recommendations=["Recording playback detected - request live verification"]
                )
            risk_score += rec_details.get("playback_score", 0.0) * 0.2

            # Layer 4: Voice Conversion Detection
            all_details["checks_performed"].append("voice_conversion_detection")
            is_converted, conv_conf, conv_details = self._detect_voice_conversion(
                audio_data, characteristics
            )
            all_details["voice_conversion_analysis"] = conv_details
            if is_converted:
                return SpoofingResult(
                    is_spoofed=True,
                    spoof_type=SpoofType.VOICE_CONVERSION,
                    confidence=conv_conf,
                    details=all_details,
                    risk_level="critical",
                    recommendations=["Voice conversion artifacts detected"]
                )
            risk_score += conv_details.get("conversion_score", 0.0) * 0.15

            # Layer 5: Liveness Detection
            all_details["checks_performed"].append("liveness_detection")
            is_live, liveness_conf, liveness_details = self._detect_liveness(
                audio_data, characteristics
            )
            all_details["liveness_analysis"] = liveness_details
            if not is_live and liveness_conf > 0.8:
                return SpoofingResult(
                    is_spoofed=True,
                    spoof_type=SpoofType.LIVENESS_FAILED,
                    confidence=liveness_conf,
                    details=all_details,
                    risk_level="high",
                    recommendations=["Liveness check failed - voice lacks natural variations"]
                )
            risk_score += (1 - liveness_details.get("liveness_score", 0.5)) * 0.2

            # Layer 6: Deepfake Detection (advanced temporal analysis)
            all_details["checks_performed"].append("deepfake_detection")
            is_deepfake, df_conf, df_details = self._detect_deepfake(
                audio_data, characteristics
            )
            all_details["deepfake_analysis"] = df_details
            if is_deepfake:
                return SpoofingResult(
                    is_spoofed=True,
                    spoof_type=SpoofType.DEEPFAKE,
                    confidence=df_conf,
                    details=all_details,
                    risk_level="critical",
                    recommendations=["Deepfake audio characteristics detected"]
                )
            risk_score += df_details.get("deepfake_score", 0.0) * 0.15

            # Store fingerprint for future replay detection
            self._store_fingerprint(audio_data)

            # Update recent embeddings for similarity tracking
            self._recent_embeddings.append({
                "fingerprint": hashlib.md5(audio_data).hexdigest(),
                "timestamp": datetime.utcnow(),
                "characteristics": characteristics
            })

            # Update user baseline if learning is enabled
            if self.enable_learning and speaker_name != "unknown":
                self._update_baseline(characteristics)

            # Determine overall risk level
            if risk_score < 0.2:
                risk_level = "low"
            elif risk_score < 0.4:
                risk_level = "medium"
            elif risk_score < 0.6:
                risk_level = "high"
            else:
                risk_level = "elevated"

            all_details["overall_risk_score"] = risk_score
            all_details["detection_passed"] = True

            return SpoofingResult(
                is_spoofed=False,
                spoof_type=SpoofType.NONE,
                confidence=1.0 - risk_score,
                details=all_details,
                risk_level=risk_level,
                recommendations=recommendations if recommendations else ["Audio passed all anti-spoofing checks"]
            )

        except Exception as e:
            logger.error(f"Anti-spoofing detection error: {e}")
            return SpoofingResult(
                is_spoofed=False,
                spoof_type=SpoofType.NONE,
                confidence=0.0,
                details={"error": str(e)},
                risk_level="unknown",
                recommendations=["Detection error - manual verification recommended"]
            )

    def _extract_characteristics(self, audio_data: bytes) -> AudioCharacteristics:
        """Extract comprehensive audio characteristics for analysis."""
        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) == 0:
                return AudioCharacteristics()

            duration_ms = (len(audio) / self.sample_rate) * 1000

            # RMS Energy
            rms_energy = float(np.sqrt(np.mean(audio ** 2)))

            # Zero Crossing Rate
            zcr = float(np.sum(np.abs(np.diff(np.signbit(audio)))) / len(audio))

            # Spectral features via FFT
            fft = np.fft.rfft(audio)
            magnitude = np.abs(fft)
            freqs = np.fft.rfftfreq(len(audio), 1.0 / self.sample_rate)

            # Spectral centroid (center of mass of spectrum)
            spectral_centroid = float(np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10))

            # Spectral bandwidth
            spectral_bandwidth = float(np.sqrt(
                np.sum(((freqs - spectral_centroid) ** 2) * magnitude) / (np.sum(magnitude) + 1e-10)
            ))

            # Spectral rolloff (frequency below which 85% of energy is contained)
            cumsum = np.cumsum(magnitude)
            rolloff_idx = np.searchsorted(cumsum, 0.85 * cumsum[-1])
            spectral_rolloff = float(freqs[min(rolloff_idx, len(freqs) - 1)])

            # Silence ratio
            silence_threshold = 0.01
            silence_ratio = float(np.sum(np.abs(audio) < silence_threshold) / len(audio))

            # SNR estimate (signal to noise ratio)
            noise_floor = np.percentile(np.abs(audio), 10)
            signal_level = np.percentile(np.abs(audio), 90)
            snr_estimate = float(20 * np.log10((signal_level + 1e-10) / (noise_floor + 1e-10)))

            # Simple pitch estimation (autocorrelation method)
            pitch_mean, pitch_variance = self._estimate_pitch(audio)

            return AudioCharacteristics(
                sample_rate=self.sample_rate,
                duration_ms=duration_ms,
                rms_energy=rms_energy,
                zero_crossing_rate=zcr,
                spectral_centroid=spectral_centroid,
                spectral_bandwidth=spectral_bandwidth,
                spectral_rolloff=spectral_rolloff,
                pitch_mean=pitch_mean,
                pitch_variance=pitch_variance,
                silence_ratio=silence_ratio,
                snr_estimate=snr_estimate
            )

        except Exception as e:
            logger.warning(f"Characteristic extraction error: {e}")
            return AudioCharacteristics()

    def _estimate_pitch(self, audio: np.ndarray) -> Tuple[float, float]:
        """Estimate pitch using autocorrelation."""
        try:
            if len(audio) < 1600:
                return 0.0, 0.0

            # Autocorrelation
            autocorr = np.correlate(audio, audio, mode='full')
            autocorr = autocorr[len(autocorr) // 2:]

            # Find peaks in autocorrelation
            min_lag = int(self.sample_rate / 500)  # 500 Hz max
            max_lag = int(self.sample_rate / 80)   # 80 Hz min

            if max_lag > len(autocorr):
                return 0.0, 0.0

            search_range = autocorr[min_lag:max_lag]
            if len(search_range) == 0:
                return 0.0, 0.0

            peak_idx = np.argmax(search_range) + min_lag
            pitch = float(self.sample_rate / peak_idx) if peak_idx > 0 else 0.0

            # Estimate variance by looking at multiple pitch periods
            pitch_estimates = []
            window_size = len(audio) // 4
            for i in range(4):
                segment = audio[i * window_size:(i + 1) * window_size]
                if len(segment) > max_lag:
                    seg_autocorr = np.correlate(segment, segment, mode='full')
                    seg_autocorr = seg_autocorr[len(seg_autocorr) // 2:]
                    seg_search = seg_autocorr[min_lag:min(max_lag, len(seg_autocorr))]
                    if len(seg_search) > 0:
                        seg_peak = np.argmax(seg_search) + min_lag
                        if seg_peak > 0:
                            pitch_estimates.append(self.sample_rate / seg_peak)

            pitch_variance = float(np.var(pitch_estimates)) if pitch_estimates else 0.0

            return pitch, pitch_variance

        except Exception:
            return 0.0, 0.0

    def _detect_replay_attack_advanced(
        self,
        audio_data: bytes,
        characteristics: AudioCharacteristics
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Advanced replay attack detection with fingerprint + temporal analysis."""
        details = {}

        # Exact fingerprint match
        fingerprint = hashlib.sha256(audio_data).hexdigest()
        self._cleanup_old_fingerprints()

        if fingerprint in self._audio_fingerprints:
            last_seen = self._audio_fingerprints[fingerprint]
            time_diff = (datetime.utcnow() - last_seen).total_seconds()
            details["exact_match"] = True
            details["time_since_last_seen"] = time_diff
            return True, 0.98, details

        # Fuzzy fingerprint match (similar audio within short timeframe)
        short_hash = hashlib.md5(audio_data).hexdigest()
        for recent in self._recent_embeddings:
            if recent["fingerprint"] == short_hash:
                time_diff = (datetime.utcnow() - recent["timestamp"]).total_seconds()
                if time_diff < 10:  # Within 10 seconds
                    details["fuzzy_match"] = True
                    details["time_diff"] = time_diff
                    return True, 0.90, details

        # Check for suspiciously identical characteristics
        for recent in self._recent_embeddings:
            recent_char = recent.get("characteristics")
            if recent_char:
                similarity = self._compare_characteristics(characteristics, recent_char)
                if similarity > 0.98:
                    time_diff = (datetime.utcnow() - recent["timestamp"]).total_seconds()
                    if time_diff < 30:
                        details["characteristic_match"] = True
                        details["similarity"] = similarity
                        details["time_diff"] = time_diff
                        return True, 0.85, details

        details["exact_match"] = False
        details["fuzzy_match"] = False
        details["replay_score"] = 0.0
        return False, 0.0, details

    def _detect_synthetic_voice_advanced(
        self,
        audio_data: bytes,
        characteristics: AudioCharacteristics
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Advanced synthetic/TTS voice detection."""
        details = {"synthesis_score": 0.0}

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < 1600:
                return False, 0.0, {"error": "audio_too_short"}

            # FFT analysis
            fft = np.fft.rfft(audio)
            magnitude = np.abs(fft)

            # Check 1: Harmonic regularity (TTS often has unnaturally regular harmonics)
            peaks = self._find_peaks(magnitude)
            harmonic_regularity = 0.0
            if len(peaks) > 3:
                peak_distances = np.diff(peaks)
                harmonic_regularity = 1 - (np.std(peak_distances) / (np.mean(peak_distances) + 1e-10))
                details["harmonic_regularity"] = float(harmonic_regularity)

            # Check 2: Silence ratio (natural speech has pauses, TTS often doesn't)
            if characteristics.silence_ratio < 0.02:
                details["missing_natural_pauses"] = True
                details["synthesis_score"] += 0.3

            # Check 3: Pitch variance (TTS often has unnaturally stable pitch)
            if characteristics.pitch_variance < 50:  # Very low pitch variation
                details["low_pitch_variance"] = True
                details["synthesis_score"] += 0.2

            # Check 4: Spectral flatness (TTS often has unusual spectral characteristics)
            geometric_mean = np.exp(np.mean(np.log(magnitude + 1e-10)))
            arithmetic_mean = np.mean(magnitude)
            spectral_flatness = geometric_mean / (arithmetic_mean + 1e-10)
            details["spectral_flatness"] = float(spectral_flatness)

            if spectral_flatness > 0.6:  # Unusually flat spectrum
                details["synthesis_score"] += 0.15

            # Check 5: Very high harmonic regularity is synthetic
            if harmonic_regularity > 0.95:
                details["synthesis_score"] += 0.35

            # Make decision
            synthesis_score = details["synthesis_score"]
            is_synthetic = synthesis_score > 0.6

            return is_synthetic, synthesis_score, details

        except Exception as e:
            details["error"] = str(e)
            return False, 0.0, details

    def _detect_recording_playback_advanced(
        self,
        audio_data: bytes,
        characteristics: AudioCharacteristics
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Advanced recording playback detection."""
        details = {"playback_score": 0.0}

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < 3200:
                return False, 0.0, {"error": "audio_too_short"}

            fft = np.fft.rfft(audio)
            magnitude = np.abs(fft)

            # Check 1: High frequency rolloff (recordings lose high frequencies)
            quarter = len(magnitude) // 4
            low_energy = np.sum(magnitude[:quarter])
            high_energy = np.sum(magnitude[3 * quarter:])

            if low_energy > 0:
                hf_ratio = high_energy / low_energy
                details["high_freq_ratio"] = float(hf_ratio)
                if hf_ratio < 0.03:  # Very little high frequency content
                    details["playback_score"] += 0.4
                    details["low_high_freq"] = True

            # Check 2: Repeated noise patterns (playback loop artifacts)
            noise_segment = audio[:500]
            correlation_scores = []
            for i in range(1, min(5, len(audio) // 500)):
                segment = audio[i * 500:(i + 1) * 500]
                if len(segment) == len(noise_segment):
                    corr = np.corrcoef(noise_segment, segment)[0, 1]
                    if not np.isnan(corr):
                        correlation_scores.append(corr)

            if correlation_scores:
                max_corr = max(correlation_scores)
                details["max_noise_correlation"] = float(max_corr)
                if max_corr > 0.8:
                    details["playback_score"] += 0.4
                    details["repeated_noise_pattern"] = True

            # Check 3: Room reverb characteristics (played recordings have double reverb)
            # Simplified check: look for echo patterns
            autocorr = np.correlate(audio[:len(audio)//2], audio[:len(audio)//2], mode='full')
            autocorr = autocorr[len(autocorr)//2:]

            # Look for secondary peaks (echo)
            peaks = []
            for i in range(int(self.sample_rate * 0.02), min(int(self.sample_rate * 0.5), len(autocorr))):
                if i > 0 and i < len(autocorr) - 1:
                    if autocorr[i] > autocorr[i-1] and autocorr[i] > autocorr[i+1]:
                        if autocorr[i] > 0.2 * autocorr[0]:
                            peaks.append(i)

            if len(peaks) > 3:
                details["multiple_echoes"] = True
                details["playback_score"] += 0.2

            playback_score = details["playback_score"]
            is_recording = playback_score > 0.5

            return is_recording, playback_score, details

        except Exception as e:
            details["error"] = str(e)
            return False, 0.0, details

    def _detect_voice_conversion(
        self,
        audio_data: bytes,
        characteristics: AudioCharacteristics
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Detect voice conversion attacks (pitch shifting, formant modification)."""
        details = {"conversion_score": 0.0}

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < 3200:
                return False, 0.0, {"error": "audio_too_short"}

            # Check 1: Unnatural pitch transitions
            # Voice conversion often has abrupt pitch changes
            window_size = len(audio) // 8
            pitch_values = []
            for i in range(8):
                segment = audio[i * window_size:(i + 1) * window_size]
                if len(segment) > 800:
                    pitch, _ = self._estimate_pitch(segment)
                    if pitch > 0:
                        pitch_values.append(pitch)

            if len(pitch_values) >= 4:
                pitch_diffs = np.diff(pitch_values)
                max_pitch_jump = np.max(np.abs(pitch_diffs))
                details["max_pitch_jump"] = float(max_pitch_jump)

                # Large sudden pitch jumps are suspicious
                if max_pitch_jump > 100:  # 100 Hz jump
                    details["conversion_score"] += 0.3
                    details["abrupt_pitch_change"] = True

            # Check 2: Formant consistency
            # Voice conversion often has inconsistent formant patterns
            fft = np.fft.rfft(audio)
            magnitude = np.abs(fft)
            freqs = np.fft.rfftfreq(len(audio), 1.0 / self.sample_rate)

            # Find formant regions (peaks in 200-4000 Hz range)
            formant_mask = (freqs >= 200) & (freqs <= 4000)
            formant_magnitude = magnitude[formant_mask]
            formant_freqs = freqs[formant_mask]

            if len(formant_magnitude) > 0:
                peaks = self._find_peaks(formant_magnitude, threshold=0.3)
                if len(peaks) >= 2:
                    # Check spacing between formants
                    formant_positions = formant_freqs[peaks]
                    formant_diffs = np.diff(formant_positions)
                    formant_regularity = np.std(formant_diffs) / (np.mean(formant_diffs) + 1e-10)
                    details["formant_regularity"] = float(formant_regularity)

                    # Very irregular formants suggest manipulation
                    if formant_regularity > 1.5:
                        details["conversion_score"] += 0.3
                        details["irregular_formants"] = True

            # Check 3: Spectral discontinuities (artifacts from processing)
            spectral_diff = np.diff(magnitude)
            discontinuity_score = np.sum(np.abs(spectral_diff) > np.std(spectral_diff) * 3) / len(spectral_diff)
            details["discontinuity_score"] = float(discontinuity_score)

            if discontinuity_score > 0.1:
                details["conversion_score"] += 0.2
                details["spectral_discontinuities"] = True

            conversion_score = details["conversion_score"]
            is_converted = conversion_score > 0.5

            return is_converted, conversion_score, details

        except Exception as e:
            details["error"] = str(e)
            return False, 0.0, details

    def _detect_liveness(
        self,
        audio_data: bytes,
        characteristics: AudioCharacteristics
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Detect liveness through micro-variations and natural speech patterns."""
        details = {"liveness_score": 0.5}

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < 3200:
                return True, 0.5, {"error": "audio_too_short", "liveness_score": 0.5}

            # Check 1: Micro energy variations (live speech has natural amplitude modulation)
            window_size = len(audio) // 20
            energy_values = []
            for i in range(20):
                segment = audio[i * window_size:(i + 1) * window_size]
                energy_values.append(np.sqrt(np.mean(segment ** 2)))

            energy_variance = np.var(energy_values) / (np.mean(energy_values) ** 2 + 1e-10)
            details["energy_variance"] = float(energy_variance)

            if energy_variance > 0.1:  # Natural variation
                details["liveness_score"] += 0.2
            elif energy_variance < 0.01:  # Suspiciously stable
                details["liveness_score"] -= 0.2

            # Check 2: Natural silence/pause patterns
            silence_threshold = 0.02
            in_silence = np.abs(audio) < silence_threshold

            # Count transitions between speech and silence
            transitions = np.sum(np.abs(np.diff(in_silence.astype(int))))
            transition_rate = transitions / (len(audio) / self.sample_rate)
            details["transition_rate"] = float(transition_rate)

            if 2 < transition_rate < 20:  # Natural range
                details["liveness_score"] += 0.15
            elif transition_rate < 1 or transition_rate > 50:  # Unnatural
                details["liveness_score"] -= 0.15

            # Check 3: Breathing indicators (low energy periods with specific characteristics)
            # Simplification: check for periodic low-energy segments
            if characteristics.silence_ratio > 0.05 and characteristics.silence_ratio < 0.4:
                details["natural_breathing_pattern"] = True
                details["liveness_score"] += 0.15
            else:
                details["natural_breathing_pattern"] = False

            # Check 4: Pitch micro-variations (natural pitch wobble)
            if characteristics.pitch_variance > 20:  # Some natural variation
                details["liveness_score"] += 0.1
                details["natural_pitch_variation"] = True

            # Clamp liveness score
            liveness_score = max(0.0, min(1.0, details["liveness_score"]))
            details["liveness_score"] = liveness_score

            is_live = liveness_score > 0.4

            return is_live, 1.0 - liveness_score, details

        except Exception as e:
            details["error"] = str(e)
            return True, 0.5, details

    def _detect_deepfake(
        self,
        audio_data: bytes,
        characteristics: AudioCharacteristics
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Detect deepfake audio through temporal inconsistencies."""
        details = {"deepfake_score": 0.0}

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < 6400:
                return False, 0.0, {"error": "audio_too_short"}

            # Check 1: Temporal consistency of spectral features
            # Deepfakes often have inconsistent spectral patterns across time
            num_windows = 8
            window_size = len(audio) // num_windows
            spectral_centroids = []
            spectral_bandwidths = []

            for i in range(num_windows):
                segment = audio[i * window_size:(i + 1) * window_size]
                fft = np.fft.rfft(segment)
                magnitude = np.abs(fft)
                freqs = np.fft.rfftfreq(len(segment), 1.0 / self.sample_rate)

                centroid = np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10)
                bandwidth = np.sqrt(np.sum(((freqs - centroid) ** 2) * magnitude) / (np.sum(magnitude) + 1e-10))

                spectral_centroids.append(centroid)
                spectral_bandwidths.append(bandwidth)

            # Check for unnatural jumps in spectral characteristics
            centroid_diffs = np.diff(spectral_centroids)
            bandwidth_diffs = np.diff(spectral_bandwidths)

            max_centroid_jump = np.max(np.abs(centroid_diffs))
            max_bandwidth_jump = np.max(np.abs(bandwidth_diffs))

            details["max_centroid_jump"] = float(max_centroid_jump)
            details["max_bandwidth_jump"] = float(max_bandwidth_jump)

            # Large sudden jumps suggest splicing/deepfake
            if max_centroid_jump > 1000:  # 1000 Hz jump
                details["deepfake_score"] += 0.3
                details["spectral_discontinuity"] = True

            if max_bandwidth_jump > 500:
                details["deepfake_score"] += 0.2

            # Check 2: Phase consistency
            # Deepfakes often have inconsistent phase relationships
            phases = []
            for i in range(num_windows):
                segment = audio[i * window_size:(i + 1) * window_size]
                fft = np.fft.rfft(segment)
                phase = np.angle(fft)
                phases.append(np.mean(np.abs(np.diff(phase))))

            phase_variance = np.var(phases)
            details["phase_variance"] = float(phase_variance)

            if phase_variance > 0.5:
                details["deepfake_score"] += 0.2
                details["phase_inconsistency"] = True

            # Check 3: Energy envelope consistency
            energy_envelope = []
            for i in range(num_windows):
                segment = audio[i * window_size:(i + 1) * window_size]
                energy_envelope.append(np.sqrt(np.mean(segment ** 2)))

            # Look for unnatural energy jumps
            energy_diffs = np.diff(energy_envelope)
            max_energy_jump = np.max(np.abs(energy_diffs)) / (np.mean(energy_envelope) + 1e-10)
            details["max_energy_jump"] = float(max_energy_jump)

            if max_energy_jump > 2.0:  # Energy doubled suddenly
                details["deepfake_score"] += 0.2
                details["energy_discontinuity"] = True

            deepfake_score = details["deepfake_score"]
            is_deepfake = deepfake_score > 0.5

            return is_deepfake, deepfake_score, details

        except Exception as e:
            details["error"] = str(e)
            return False, 0.0, details

    def _find_peaks(self, data: np.ndarray, threshold: float = 0.5) -> List[int]:
        """Find peaks in frequency magnitude."""
        if len(data) < 3:
            return []

        threshold_val = np.max(data) * threshold
        peaks = []

        for i in range(1, len(data) - 1):
            if data[i] > data[i - 1] and data[i] > data[i + 1] and data[i] > threshold_val:
                peaks.append(i)

        return peaks

    def _compare_characteristics(
        self,
        char1: AudioCharacteristics,
        char2: AudioCharacteristics
    ) -> float:
        """Compare two audio characteristic sets for similarity."""
        try:
            features1 = np.array([
                char1.rms_energy,
                char1.zero_crossing_rate,
                char1.spectral_centroid / 1000,  # Normalize
                char1.spectral_bandwidth / 1000,
                char1.pitch_mean / 100,
                char1.silence_ratio
            ])

            features2 = np.array([
                char2.rms_energy,
                char2.zero_crossing_rate,
                char2.spectral_centroid / 1000,
                char2.spectral_bandwidth / 1000,
                char2.pitch_mean / 100,
                char2.silence_ratio
            ])

            # Cosine similarity
            dot = np.dot(features1, features2)
            norm1 = np.linalg.norm(features1)
            norm2 = np.linalg.norm(features2)

            if norm1 > 0 and norm2 > 0:
                return float(dot / (norm1 * norm2))
            return 0.0

        except Exception:
            return 0.0

    def _store_fingerprint(self, audio_data: bytes):
        """Store audio fingerprint for replay detection."""
        fingerprint = hashlib.sha256(audio_data).hexdigest()
        self._audio_fingerprints[fingerprint] = datetime.utcnow()

        if len(self._audio_fingerprints) > self._cleanup_threshold:
            self._cleanup_old_fingerprints()

    def _cleanup_old_fingerprints(self):
        """Remove expired fingerprints."""
        cutoff = datetime.utcnow() - timedelta(seconds=self.fingerprint_cache_ttl)
        expired = [fp for fp, ts in self._audio_fingerprints.items() if ts < cutoff]
        for fp in expired:
            del self._audio_fingerprints[fp]

    def _update_baseline(self, characteristics: AudioCharacteristics):
        """Update user baseline characteristics for adaptive detection."""
        self._baseline_samples.append(characteristics)

        # Keep only recent samples
        if len(self._baseline_samples) > 50:
            self._baseline_samples = self._baseline_samples[-50:]

        # Update baseline with running average
        if len(self._baseline_samples) >= 5:
            self._user_baseline = AudioCharacteristics(
                rms_energy=np.mean([s.rms_energy for s in self._baseline_samples]),
                zero_crossing_rate=np.mean([s.zero_crossing_rate for s in self._baseline_samples]),
                spectral_centroid=np.mean([s.spectral_centroid for s in self._baseline_samples]),
                spectral_bandwidth=np.mean([s.spectral_bandwidth for s in self._baseline_samples]),
                pitch_mean=np.mean([s.pitch_mean for s in self._baseline_samples]),
                pitch_variance=np.mean([s.pitch_variance for s in self._baseline_samples]),
                silence_ratio=np.mean([s.silence_ratio for s in self._baseline_samples]),
                snr_estimate=np.mean([s.snr_estimate for s in self._baseline_samples])
            )

    def clear_cache(self):
        """Clear all cached data."""
        self._audio_fingerprints.clear()
        self._recent_embeddings.clear()
        logger.info("Anti-spoofing cache cleared")

    def get_statistics(self) -> Dict[str, Any]:
        """Get detection statistics including physics-aware analysis."""
        stats = {
            "fingerprints_cached": len(self._audio_fingerprints),
            "recent_embeddings": len(self._recent_embeddings),
            "baseline_samples": len(self._baseline_samples),
            "has_user_baseline": self._user_baseline is not None,
            "learning_enabled": self.enable_learning,
            "cache_ttl_seconds": self.fingerprint_cache_ttl,
            "detection_layers": 7 if self.enable_physics else 6,
            "physics_enabled": self.enable_physics
        }

        # Add physics statistics if enabled
        if self.enable_physics and self._physics_extractor:
            physics_stats = self._physics_extractor.get_statistics()
            stats["physics"] = {
                "baseline_vtl_cm": physics_stats.get("baseline_vtl_cm"),
                "baseline_rt60_seconds": physics_stats.get("baseline_rt60_seconds"),
                "vtl_samples": physics_stats.get("vtl_samples", 0),
                "config": physics_stats.get("config", {})
            }

        return stats

    def get_physics_extractor(self) -> Optional[PhysicsAwareFeatureExtractor]:
        """Get the physics-aware feature extractor if enabled."""
        return self._physics_extractor if self.enable_physics else None
