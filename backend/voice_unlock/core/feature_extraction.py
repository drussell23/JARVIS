"""
Voice Feature Extraction for Voice Unlock System

Provides audio feature extraction for speaker verification and ML models.

Physics-Aware Voice Authentication v2.0:
- Reverberation analysis (RT60, double-reverb detection)
- Vocal tract length estimation from formant frequencies
- Room impulse response analysis
- Doppler effect detection for movement patterns
- Bayesian confidence fusion with environmental context

Mathematical Foundation:
- VTL = c / (2 * Δf) where c = speed of sound (343 m/s), Δf = formant spacing
- RT60 estimation via Schroeder backward integration
- Bayesian P(authentic|evidence) = P(evidence|authentic) * P(authentic) / P(evidence)
"""

import logging
import os
import numpy as np
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import asyncio
from functools import lru_cache

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - Environment-driven, no hardcoding
# =============================================================================

class PhysicsConfig:
    """Physics-aware authentication configuration from environment."""

    # Speed of sound in air at 20°C (configurable for different conditions)
    SPEED_OF_SOUND_MPS = float(os.getenv("PHYSICS_SPEED_OF_SOUND", "343.0"))

    # Vocal tract length estimation parameters
    VTL_MIN_CM = float(os.getenv("VTL_MIN_CM", "12.0"))  # Female minimum
    VTL_MAX_CM = float(os.getenv("VTL_MAX_CM", "20.0"))  # Male maximum
    VTL_TOLERANCE_CM = float(os.getenv("VTL_TOLERANCE_CM", "1.5"))

    # Reverberation parameters
    RT60_MIN_SECONDS = float(os.getenv("RT60_MIN_SECONDS", "0.1"))
    RT60_MAX_SECONDS = float(os.getenv("RT60_MAX_SECONDS", "2.0"))
    DOUBLE_REVERB_THRESHOLD = float(os.getenv("DOUBLE_REVERB_THRESHOLD", "0.7"))

    # Doppler effect parameters
    MAX_DOPPLER_SHIFT_HZ = float(os.getenv("MAX_DOPPLER_SHIFT_HZ", "5.0"))
    NATURAL_MOVEMENT_HZ = float(os.getenv("NATURAL_MOVEMENT_HZ", "2.0"))

    # Bayesian fusion weights (prior probabilities)
    PRIOR_AUTHENTIC = float(os.getenv("BAYESIAN_PRIOR_AUTHENTIC", "0.85"))
    PRIOR_SPOOF = float(os.getenv("BAYESIAN_PRIOR_SPOOF", "0.15"))

    # Formant frequency ranges (Hz) for vocal tract analysis
    F1_MIN = float(os.getenv("FORMANT_F1_MIN", "250.0"))
    F1_MAX = float(os.getenv("FORMANT_F1_MAX", "900.0"))
    F2_MIN = float(os.getenv("FORMANT_F2_MIN", "850.0"))
    F2_MAX = float(os.getenv("FORMANT_F2_MAX", "2500.0"))
    F3_MIN = float(os.getenv("FORMANT_F3_MIN", "2000.0"))
    F3_MAX = float(os.getenv("FORMANT_F3_MAX", "3500.0"))


class PhysicsConfidenceLevel(str, Enum):
    """Physics-based confidence levels."""
    PHYSICS_VERIFIED = "physics_verified"  # All physics checks pass
    PHYSICS_LIKELY = "physics_likely"       # Most physics checks pass
    PHYSICS_UNCERTAIN = "physics_uncertain" # Mixed results
    PHYSICS_SUSPICIOUS = "physics_suspicious"  # Physics anomalies detected
    PHYSICS_FAILED = "physics_failed"       # Physics violation detected


# =============================================================================
# DATA CLASSES FOR PHYSICS-AWARE FEATURES
# =============================================================================

@dataclass
class ReverbAnalysis:
    """Results of reverberation analysis."""
    rt60_estimated: float = 0.0  # Reverberation time in seconds
    early_decay_time: float = 0.0  # EDT in seconds
    clarity_c50: float = 0.0  # Clarity index (ratio early/late energy)
    double_reverb_detected: bool = False
    double_reverb_confidence: float = 0.0
    room_size_estimate: str = "unknown"  # small, medium, large, open
    impulse_response_peaks: List[float] = field(default_factory=list)
    is_consistent_with_baseline: bool = True
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VocalTractAnalysis:
    """Results of vocal tract length analysis."""
    vtl_estimated_cm: float = 0.0  # Estimated vocal tract length
    formant_frequencies: List[float] = field(default_factory=list)  # F1, F2, F3, F4
    formant_spacing_hz: float = 0.0  # Average spacing between formants
    formant_consistency: float = 0.0  # How consistent formants are with VTL model
    is_within_human_range: bool = True
    is_consistent_with_baseline: bool = True
    vtl_deviation_cm: float = 0.0  # Deviation from baseline
    speaker_sex_estimate: str = "unknown"  # male, female, unknown
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DopplerAnalysis:
    """Results of Doppler effect analysis for movement detection."""
    frequency_drift_hz: float = 0.0  # Total frequency drift
    movement_detected: bool = False
    movement_pattern: str = "none"  # none, natural, static, erratic
    velocity_estimate_mps: float = 0.0  # Estimated source velocity
    is_natural_movement: bool = True
    stability_score: float = 0.0  # 0-1, higher = more stable
    micro_movements: int = 0  # Count of natural micro-movements
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoomAcousticSignature:
    """Room acoustic signature for environment verification."""
    signature_hash: str = ""
    background_noise_profile: np.ndarray = field(default_factory=lambda: np.array([]))
    reverb_fingerprint: np.ndarray = field(default_factory=lambda: np.array([]))
    ambient_frequency_peaks: List[float] = field(default_factory=list)
    estimated_room_dimensions: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PhysicsAwareFeatures:
    """Complete physics-aware feature set."""
    # Reverberation features
    reverb_analysis: ReverbAnalysis = field(default_factory=ReverbAnalysis)

    # Vocal tract features
    vocal_tract: VocalTractAnalysis = field(default_factory=VocalTractAnalysis)

    # Doppler/movement features
    doppler: DopplerAnalysis = field(default_factory=DopplerAnalysis)

    # Room signature
    room_signature: Optional[RoomAcousticSignature] = None

    # Overall physics confidence
    physics_confidence: float = 0.0
    physics_level: PhysicsConfidenceLevel = PhysicsConfidenceLevel.PHYSICS_UNCERTAIN

    # Bayesian fusion result
    bayesian_authentic_probability: float = 0.0
    bayesian_spoof_probability: float = 0.0

    # Timestamps and metadata
    extraction_time_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Detailed breakdown
    physics_scores: Dict[str, float] = field(default_factory=dict)
    anomalies_detected: List[str] = field(default_factory=list)


@dataclass
class VoiceFeatures:
    """Container for extracted voice features."""
    # Spectral features
    mfcc: Optional[np.ndarray] = None
    spectral_centroid: float = 0.0
    spectral_bandwidth: float = 0.0
    spectral_rolloff: float = 0.0

    # Prosodic features
    fundamental_frequency: float = 0.0
    pitch_variance: float = 0.0
    speech_rate: float = 0.0

    # Energy features
    rms_energy: float = 0.0
    zero_crossing_rate: float = 0.0

    # Quality metrics
    snr_db: float = 0.0
    duration_seconds: float = 0.0


class VoiceFeatureExtractor:
    """
    Extract audio features for voice authentication and ML processing.

    Features extracted:
    - MFCC (Mel-Frequency Cepstral Coefficients)
    - Spectral features (centroid, bandwidth, rolloff)
    - Prosodic features (F0, pitch variance, speech rate)
    - Energy features (RMS, ZCR)
    """

    def __init__(self, sample_rate: int = 16000, n_mfcc: int = 13):
        self.sample_rate = sample_rate
        self.n_mfcc = n_mfcc
        self._initialized = False

    def extract_features(
        self,
        audio_data: bytes,
        normalize: bool = True
    ) -> VoiceFeatures:
        """
        Extract voice features from audio data.

        Args:
            audio_data: Raw audio bytes (16-bit PCM)
            normalize: Whether to normalize audio before processing

        Returns:
            VoiceFeatures containing extracted features
        """
        features = VoiceFeatures()

        try:
            # Convert bytes to numpy array
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)

            if len(audio) < 1600:  # Less than 0.1 second
                return features

            # Normalize
            if normalize:
                audio = audio / 32768.0

            # Calculate duration
            features.duration_seconds = len(audio) / self.sample_rate

            # RMS Energy
            features.rms_energy = float(np.sqrt(np.mean(audio ** 2)))

            # Zero Crossing Rate
            zero_crossings = np.sum(np.abs(np.diff(np.sign(audio)))) / 2
            features.zero_crossing_rate = float(zero_crossings / len(audio))

            # Spectral features
            fft = np.fft.rfft(audio)
            magnitude = np.abs(fft)
            freqs = np.fft.rfftfreq(len(audio), 1 / self.sample_rate)

            # Spectral centroid
            if np.sum(magnitude) > 0:
                features.spectral_centroid = float(
                    np.sum(freqs * magnitude) / np.sum(magnitude)
                )

            # Spectral bandwidth
            if features.spectral_centroid > 0:
                features.spectral_bandwidth = float(
                    np.sqrt(
                        np.sum(((freqs - features.spectral_centroid) ** 2) * magnitude) /
                        (np.sum(magnitude) + 1e-10)
                    )
                )

            # Spectral rolloff (95% of energy)
            cumulative_energy = np.cumsum(magnitude)
            if cumulative_energy[-1] > 0:
                rolloff_idx = np.searchsorted(cumulative_energy, 0.95 * cumulative_energy[-1])
                if rolloff_idx < len(freqs):
                    features.spectral_rolloff = float(freqs[rolloff_idx])

            # Fundamental frequency (F0) using autocorrelation
            features.fundamental_frequency = self._estimate_f0(audio)

            # SNR estimation
            features.snr_db = self._estimate_snr(audio)

            # Simple MFCC-like features (without librosa dependency)
            features.mfcc = self._simple_mfcc(audio)

        except Exception as e:
            logger.debug(f"Feature extraction error: {e}")

        return features

    def _estimate_f0(self, audio: np.ndarray) -> float:
        """Estimate fundamental frequency using autocorrelation."""
        try:
            min_period = int(self.sample_rate / 400)  # 400 Hz max
            max_period = int(self.sample_rate / 75)   # 75 Hz min

            corr = np.correlate(audio[:4096], audio[:4096], mode='full')
            corr = corr[len(corr) // 2:]

            if max_period < len(corr):
                search_range = corr[min_period:max_period]
                if len(search_range) > 0:
                    peak_idx = np.argmax(search_range) + min_period
                    return float(self.sample_rate / peak_idx)
        except Exception:
            pass
        return 0.0

    def _estimate_snr(self, audio: np.ndarray) -> float:
        """Estimate signal-to-noise ratio."""
        try:
            noise_samples = int(len(audio) * 0.1)
            noise = audio[:noise_samples]
            signal = audio[noise_samples:]

            noise_power = np.mean(noise ** 2) + 1e-10
            signal_power = np.mean(signal ** 2) + 1e-10

            return float(10 * np.log10(signal_power / noise_power))
        except Exception:
            return 15.0

    def _simple_mfcc(self, audio: np.ndarray, n_mels: int = 40) -> np.ndarray:
        """Simple MFCC-like feature extraction without librosa."""
        try:
            # Use FFT on overlapping frames
            frame_size = 512
            hop_size = 256
            n_frames = max(1, (len(audio) - frame_size) // hop_size)

            mfccs = []
            for i in range(min(n_frames, 20)):  # Limit frames
                start = i * hop_size
                frame = audio[start:start + frame_size]
                if len(frame) < frame_size:
                    frame = np.pad(frame, (0, frame_size - len(frame)))

                # Window and FFT
                windowed = frame * np.hanning(frame_size)
                fft = np.fft.rfft(windowed)
                power = np.abs(fft) ** 2

                # Simple mel-scale approximation
                mel_weights = np.linspace(0, len(power) - 1, n_mels + 2).astype(int)
                mel_spec = []
                for j in range(n_mels):
                    mel_spec.append(np.sum(power[mel_weights[j]:mel_weights[j + 1]]))

                # Log and DCT approximation
                mel_spec = np.log(np.array(mel_spec) + 1e-10)
                mfcc = np.fft.rfft(mel_spec).real[:self.n_mfcc]
                mfccs.append(mfcc)

            return np.mean(mfccs, axis=0) if mfccs else np.zeros(self.n_mfcc)

        except Exception:
            return np.zeros(self.n_mfcc)

    def get_feature_vector(self, features: VoiceFeatures) -> np.ndarray:
        """Convert VoiceFeatures to a flat numpy array for ML models."""
        vector = [
            features.spectral_centroid,
            features.spectral_bandwidth,
            features.spectral_rolloff,
            features.fundamental_frequency,
            features.pitch_variance,
            features.speech_rate,
            features.rms_energy,
            features.zero_crossing_rate,
            features.snr_db,
            features.duration_seconds
        ]

        if features.mfcc is not None:
            vector.extend(features.mfcc.tolist())

        return np.array(vector, dtype=np.float32)


# =============================================================================
# PHYSICS-AWARE VOICE AUTHENTICATION ANALYZERS
# =============================================================================

class ReverbAnalyzer:
    """
    Reverberation analysis for replay attack detection.

    Mathematical Foundation:
    - RT60: Time for sound to decay by 60 dB
    - Schroeder backward integration: E(t) = ∫[t,∞] p²(τ)dτ
    - Double-reverb detection: Recorded audio played back acquires room reverb twice

    When audio is recorded and played back in a room:
    1. Original recording has room A's reverb
    2. Playback adds room B's reverb
    3. Result has convolved reverb characteristics (unnatural RT60 curve)
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.config = PhysicsConfig()
        self._baseline_rt60: Optional[float] = None
        self._baseline_room_signatures: List[RoomAcousticSignature] = []

    async def analyze_reverberation_async(
        self,
        audio_data: bytes,
        baseline_rt60: Optional[float] = None
    ) -> ReverbAnalysis:
        """Async reverberation analysis."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.analyze_reverberation,
            audio_data,
            baseline_rt60
        )

    def analyze_reverberation(
        self,
        audio_data: bytes,
        baseline_rt60: Optional[float] = None
    ) -> ReverbAnalysis:
        """
        Comprehensive reverberation analysis.

        Detects:
        1. Room characteristics (RT60, EDT)
        2. Double-reverb artifacts from playback attacks
        3. Room size estimation
        4. Consistency with learned baseline
        """
        result = ReverbAnalysis()

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < self.sample_rate:  # Need at least 1 second
                return result

            # Estimate RT60 using energy decay curve (Schroeder method)
            result.rt60_estimated = self._estimate_rt60_schroeder(audio)

            # Estimate early decay time (first 10 dB of decay)
            result.early_decay_time = self._estimate_edt(audio)

            # Calculate clarity index C50 (early-to-late energy ratio)
            result.clarity_c50 = self._calculate_clarity_c50(audio)

            # Detect double-reverb (key for replay attack detection)
            double_reverb, dr_confidence = self._detect_double_reverb(audio)
            result.double_reverb_detected = double_reverb
            result.double_reverb_confidence = dr_confidence

            # Estimate room size from RT60
            result.room_size_estimate = self._estimate_room_size(result.rt60_estimated)

            # Find impulse response peaks (reflections)
            result.impulse_response_peaks = self._find_ir_peaks(audio)

            # Check consistency with baseline
            if baseline_rt60 is not None or self._baseline_rt60 is not None:
                ref_rt60 = baseline_rt60 or self._baseline_rt60
                deviation = abs(result.rt60_estimated - ref_rt60)
                # Allow 20% deviation
                result.is_consistent_with_baseline = deviation < (ref_rt60 * 0.2)

            result.details = {
                "rt60_seconds": result.rt60_estimated,
                "edt_seconds": result.early_decay_time,
                "clarity_c50_db": result.clarity_c50,
                "double_reverb_confidence": dr_confidence,
                "room_size": result.room_size_estimate,
                "ir_peak_count": len(result.impulse_response_peaks),
                "baseline_consistent": result.is_consistent_with_baseline
            }

        except Exception as e:
            logger.debug(f"Reverberation analysis error: {e}")
            result.details["error"] = str(e)

        return result

    def _estimate_rt60_schroeder(self, audio: np.ndarray) -> float:
        """
        Estimate RT60 using Schroeder backward integration.

        The Schroeder curve is computed as:
        E(t) = ∫[t,∞] |h(τ)|² dτ

        Where h(τ) is the impulse response.
        """
        try:
            # Square the signal for energy
            energy = audio ** 2

            # Backward integration (Schroeder method)
            schroeder = np.cumsum(energy[::-1])[::-1]
            schroeder = schroeder / (schroeder[0] + 1e-10)

            # Convert to dB
            schroeder_db = 10 * np.log10(schroeder + 1e-10)

            # Find -60 dB point (or extrapolate from -20 dB)
            # Using -20 dB point is more robust for noisy signals
            db_20_idx = np.searchsorted(-schroeder_db, 20)

            if db_20_idx > 0 and db_20_idx < len(schroeder_db):
                # Extrapolate to -60 dB
                t20 = db_20_idx / self.sample_rate
                rt60 = t20 * 3  # -60 dB = 3 * -20 dB time
                return float(np.clip(rt60, self.config.RT60_MIN_SECONDS, self.config.RT60_MAX_SECONDS))

            return 0.3  # Default moderate reverb

        except Exception:
            return 0.3

    def _estimate_edt(self, audio: np.ndarray) -> float:
        """Estimate Early Decay Time (first 10 dB of decay)."""
        try:
            energy = audio ** 2
            schroeder = np.cumsum(energy[::-1])[::-1]
            schroeder = schroeder / (schroeder[0] + 1e-10)
            schroeder_db = 10 * np.log10(schroeder + 1e-10)

            # Find -10 dB point
            db_10_idx = np.searchsorted(-schroeder_db, 10)

            if db_10_idx > 0:
                edt = (db_10_idx / self.sample_rate) * 6  # Extrapolate to 60 dB
                return float(edt)

            return 0.2

        except Exception:
            return 0.2

    def _calculate_clarity_c50(self, audio: np.ndarray) -> float:
        """
        Calculate Clarity index C50.

        C50 = 10 * log10(E_early / E_late)

        Where:
        - E_early = energy in first 50ms
        - E_late = energy after 50ms
        """
        try:
            samples_50ms = int(0.05 * self.sample_rate)
            energy = audio ** 2

            early_energy = np.sum(energy[:samples_50ms])
            late_energy = np.sum(energy[samples_50ms:])

            if late_energy > 1e-10:
                c50 = 10 * np.log10((early_energy + 1e-10) / (late_energy + 1e-10))
                return float(c50)

            return 10.0  # Very clear (anechoic-like)

        except Exception:
            return 0.0

    def _detect_double_reverb(self, audio: np.ndarray) -> Tuple[bool, float]:
        """
        Detect double-reverb characteristic of replay attacks.

        When recorded audio is played back:
        1. Recording has reverb from original room
        2. Playback adds reverb from current room
        3. Results in unnatural reverb decay curve

        Detection method:
        - Analyze reverb decay shape
        - Double-reverb shows multi-exponential decay
        - Check for secondary decay slope changes
        """
        try:
            energy = audio ** 2
            schroeder = np.cumsum(energy[::-1])[::-1]
            schroeder = schroeder / (schroeder[0] + 1e-10)
            schroeder_db = 10 * np.log10(schroeder + 1e-10)

            # Analyze decay curve slope in segments
            # Natural reverb: single exponential decay
            # Double reverb: multi-exponential with inflection points

            segment_length = len(schroeder_db) // 5
            slopes = []

            for i in range(4):
                start = i * segment_length
                end = (i + 1) * segment_length
                segment = schroeder_db[start:end]

                if len(segment) > 10:
                    # Linear fit for slope
                    x = np.arange(len(segment))
                    slope = np.polyfit(x, segment, 1)[0]
                    slopes.append(slope)

            if len(slopes) >= 3:
                # Check for slope changes (inflection points)
                slope_changes = np.abs(np.diff(slopes))
                max_change = np.max(slope_changes)
                avg_slope = np.mean(np.abs(slopes))

                # Double reverb shows significant slope changes
                if avg_slope > 0:
                    inflection_ratio = max_change / avg_slope
                    confidence = min(1.0, inflection_ratio / 2.0)

                    # Threshold for double-reverb detection
                    is_double = inflection_ratio > self.config.DOUBLE_REVERB_THRESHOLD

                    return is_double, float(confidence)

            return False, 0.0

        except Exception:
            return False, 0.0

    def _estimate_room_size(self, rt60: float) -> str:
        """Estimate room size from RT60."""
        if rt60 < 0.2:
            return "small"  # Small room or treated space
        elif rt60 < 0.5:
            return "medium"  # Medium room
        elif rt60 < 1.0:
            return "large"  # Large room
        else:
            return "open"  # Very large or open space

    def _find_ir_peaks(self, audio: np.ndarray) -> List[float]:
        """Find peaks in impulse response (room reflections)."""
        try:
            # Autocorrelation to find reflection patterns
            autocorr = np.correlate(audio[:len(audio)//2], audio[:len(audio)//2], mode='full')
            autocorr = autocorr[len(autocorr)//2:]
            autocorr = autocorr / (autocorr[0] + 1e-10)

            # Find peaks (reflections)
            peaks = []
            min_distance = int(0.001 * self.sample_rate)  # 1ms minimum

            for i in range(min_distance, min(len(autocorr) - 1, int(0.5 * self.sample_rate))):
                if autocorr[i] > autocorr[i-1] and autocorr[i] > autocorr[i+1]:
                    if autocorr[i] > 0.1:  # Significant peak
                        peak_time_ms = (i / self.sample_rate) * 1000
                        peaks.append(float(peak_time_ms))

            return peaks[:10]  # Return top 10 peaks

        except Exception:
            return []

    def update_baseline(self, rt60: float):
        """Update baseline RT60 for consistency checking."""
        if self._baseline_rt60 is None:
            self._baseline_rt60 = rt60
        else:
            # Exponential moving average
            self._baseline_rt60 = 0.9 * self._baseline_rt60 + 0.1 * rt60


class VocalTractAnalyzer:
    """
    Vocal tract length estimation for speaker biometrics.

    Mathematical Foundation:
    - The vocal tract acts as an acoustic tube
    - Formant frequencies relate to vocal tract length: VTL = c / (2 * Δf)
    - VTL is a physical characteristic that's extremely difficult to spoof

    Formula derivation:
    - For quarter-wave resonator: f_n = (2n-1) * c / (4L)
    - For formant spacing: Δf ≈ c / (2L)
    - Rearranging: L = c / (2 * Δf)

    Typical VTL ranges:
    - Adult male: 16-20 cm
    - Adult female: 13-16 cm
    - Children: 10-13 cm
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.config = PhysicsConfig()
        self._baseline_vtl: Optional[float] = None
        self._vtl_history: List[float] = []

    async def analyze_vocal_tract_async(
        self,
        audio_data: bytes,
        baseline_vtl: Optional[float] = None
    ) -> VocalTractAnalysis:
        """Async vocal tract analysis."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.analyze_vocal_tract,
            audio_data,
            baseline_vtl
        )

    def analyze_vocal_tract(
        self,
        audio_data: bytes,
        baseline_vtl: Optional[float] = None
    ) -> VocalTractAnalysis:
        """
        Comprehensive vocal tract length analysis.

        This is a powerful anti-spoofing measure because:
        1. VTL is a physical characteristic unique to each person
        2. Voice conversion attacks often fail to match VTL
        3. TTS systems typically don't model VTL accurately
        """
        result = VocalTractAnalysis()

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < self.sample_rate // 2:  # Need at least 0.5 seconds
                return result

            # Extract formant frequencies using LPC
            formants = self._extract_formants_lpc(audio)
            result.formant_frequencies = formants

            if len(formants) >= 3:
                # Calculate formant spacing
                spacings = np.diff(formants)
                result.formant_spacing_hz = float(np.mean(spacings))

                # Estimate VTL from formant spacing
                # VTL = c / (2 * Δf)
                if result.formant_spacing_hz > 0:
                    vtl_meters = self.config.SPEED_OF_SOUND_MPS / (2 * result.formant_spacing_hz)
                    result.vtl_estimated_cm = float(vtl_meters * 100)

                # Check if VTL is within human range
                result.is_within_human_range = (
                    self.config.VTL_MIN_CM <= result.vtl_estimated_cm <= self.config.VTL_MAX_CM
                )

                # Estimate speaker sex from VTL
                if result.vtl_estimated_cm < 14.5:
                    result.speaker_sex_estimate = "female"
                elif result.vtl_estimated_cm > 16.5:
                    result.speaker_sex_estimate = "male"
                else:
                    result.speaker_sex_estimate = "unknown"

                # Calculate formant consistency with expected VTL model
                result.formant_consistency = self._calculate_formant_consistency(
                    formants, result.vtl_estimated_cm
                )

                # Check consistency with baseline
                ref_vtl = baseline_vtl or self._baseline_vtl
                if ref_vtl is not None:
                    result.vtl_deviation_cm = abs(result.vtl_estimated_cm - ref_vtl)
                    result.is_consistent_with_baseline = (
                        result.vtl_deviation_cm <= self.config.VTL_TOLERANCE_CM
                    )

            result.details = {
                "vtl_cm": result.vtl_estimated_cm,
                "formants_hz": formants,
                "formant_spacing_hz": result.formant_spacing_hz,
                "formant_consistency": result.formant_consistency,
                "is_human_range": result.is_within_human_range,
                "sex_estimate": result.speaker_sex_estimate,
                "baseline_consistent": result.is_consistent_with_baseline,
                "vtl_deviation_cm": result.vtl_deviation_cm
            }

        except Exception as e:
            logger.debug(f"Vocal tract analysis error: {e}")
            result.details["error"] = str(e)

        return result

    def _extract_formants_lpc(self, audio: np.ndarray, num_formants: int = 4) -> List[float]:
        """
        Extract formant frequencies using Linear Predictive Coding (LPC).

        LPC models the vocal tract as an all-pole filter:
        H(z) = G / (1 - Σ a_k * z^(-k))

        Formants are found at the angles of the poles.
        """
        try:
            # Pre-emphasis to boost high frequencies
            pre_emphasis = 0.97
            audio_emphasized = np.append(audio[0], audio[1:] - pre_emphasis * audio[:-1])

            # Window the signal
            window = np.hanning(len(audio_emphasized))
            windowed = audio_emphasized * window

            # LPC order (rule of thumb: 2 + sample_rate/1000)
            lpc_order = min(2 + self.sample_rate // 1000, 16)

            # Compute autocorrelation
            autocorr = np.correlate(windowed, windowed, mode='full')
            autocorr = autocorr[len(autocorr)//2:len(autocorr)//2 + lpc_order + 1]

            # Levinson-Durbin recursion for LPC coefficients
            lpc_coeffs = self._levinson_durbin(autocorr, lpc_order)

            # Find roots of LPC polynomial
            roots = np.roots(np.concatenate([[1], -lpc_coeffs]))

            # Convert to frequencies
            formants = []
            for root in roots:
                if np.imag(root) > 0:  # Only positive frequencies
                    frequency = np.abs(np.arctan2(np.imag(root), np.real(root)))
                    frequency = frequency * self.sample_rate / (2 * np.pi)

                    # Bandwidth (from root distance to unit circle)
                    bandwidth = -np.log(np.abs(root)) * self.sample_rate / np.pi

                    # Filter by formant ranges and bandwidth
                    if 200 < frequency < 4000 and bandwidth < 500:
                        formants.append(frequency)

            # Sort and return top formants
            formants = sorted(set(formants))

            # Validate formants are in expected ranges
            validated = []
            ranges = [
                (self.config.F1_MIN, self.config.F1_MAX),
                (self.config.F2_MIN, self.config.F2_MAX),
                (self.config.F3_MIN, self.config.F3_MAX),
                (3000, 4500)  # F4 range
            ]

            for i, (f_min, f_max) in enumerate(ranges):
                for f in formants:
                    if f_min <= f <= f_max:
                        validated.append(f)
                        break

            return [float(f) for f in validated[:num_formants]]

        except Exception as e:
            logger.debug(f"Formant extraction error: {e}")
            return []

    def _levinson_durbin(self, autocorr: np.ndarray, order: int) -> np.ndarray:
        """Levinson-Durbin recursion for LPC coefficients."""
        try:
            n = order
            a = np.zeros(n)
            e = autocorr[0]

            for i in range(n):
                # Reflection coefficient
                lambda_val = autocorr[i + 1]
                for j in range(i):
                    lambda_val -= a[j] * autocorr[i - j]

                k = lambda_val / (e + 1e-10)

                # Update coefficients
                a_new = np.zeros(n)
                a_new[i] = k
                for j in range(i):
                    a_new[j] = a[j] - k * a[i - 1 - j]
                a = a_new

                # Update error
                e = e * (1 - k * k)

            return a

        except Exception:
            return np.zeros(order)

    def _calculate_formant_consistency(
        self,
        formants: List[float],
        vtl_cm: float
    ) -> float:
        """
        Calculate how well formants match expected spacing for estimated VTL.

        Expected formant frequencies for a uniform tube:
        f_n = (2n-1) * c / (4L)
        """
        try:
            if len(formants) < 2 or vtl_cm <= 0:
                return 0.0

            vtl_m = vtl_cm / 100
            c = self.config.SPEED_OF_SOUND_MPS

            # Expected formants for uniform tube model
            expected = []
            for n in range(1, len(formants) + 1):
                expected.append((2*n - 1) * c / (4 * vtl_m))

            # Calculate normalized error
            errors = []
            for actual, exp in zip(formants, expected):
                if exp > 0:
                    error = abs(actual - exp) / exp
                    errors.append(error)

            if errors:
                avg_error = np.mean(errors)
                consistency = max(0, 1 - avg_error)
                return float(consistency)

            return 0.0

        except Exception:
            return 0.0

    def update_baseline(self, vtl_cm: float):
        """Update baseline VTL for consistency checking."""
        self._vtl_history.append(vtl_cm)
        if len(self._vtl_history) > 50:
            self._vtl_history = self._vtl_history[-50:]

        if len(self._vtl_history) >= 5:
            self._baseline_vtl = float(np.median(self._vtl_history))


class DopplerAnalyzer:
    """
    Doppler effect analysis for natural movement detection.

    Mathematical Foundation:
    - Doppler shift: Δf = f * (v/c)
    - Where v = source velocity, c = speed of sound

    Application:
    - Live speakers naturally move their heads while speaking
    - Recordings play back with static frequency characteristics
    - Detecting natural Doppler patterns indicates liveness
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.config = PhysicsConfig()

    async def analyze_doppler_async(self, audio_data: bytes) -> DopplerAnalysis:
        """Async Doppler analysis."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.analyze_doppler, audio_data)

    def analyze_doppler(self, audio_data: bytes) -> DopplerAnalysis:
        """
        Analyze Doppler effects in voice signal.

        Detects:
        1. Natural micro-movements during speech
        2. Static recordings (no Doppler)
        3. Unnatural movement patterns
        """
        result = DopplerAnalysis()

        try:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio) < self.sample_rate // 2:
                return result

            # Analyze frequency drift over time
            drift_analysis = self._analyze_frequency_drift(audio)
            result.frequency_drift_hz = drift_analysis["total_drift"]

            # Count micro-movements
            result.micro_movements = drift_analysis["micro_movements"]

            # Estimate velocity from Doppler shift
            # v = (Δf / f) * c
            if drift_analysis["reference_freq"] > 0:
                result.velocity_estimate_mps = (
                    result.frequency_drift_hz / drift_analysis["reference_freq"]
                ) * self.config.SPEED_OF_SOUND_MPS

            # Classify movement pattern
            result.movement_pattern = self._classify_movement_pattern(drift_analysis)
            result.movement_detected = result.movement_pattern != "none"

            # Determine if movement is natural
            result.is_natural_movement = result.movement_pattern in ["natural", "subtle"]

            # Calculate stability score
            result.stability_score = drift_analysis["stability"]

            result.details = {
                "frequency_drift_hz": result.frequency_drift_hz,
                "micro_movements": result.micro_movements,
                "velocity_mps": result.velocity_estimate_mps,
                "pattern": result.movement_pattern,
                "is_natural": result.is_natural_movement,
                "stability": result.stability_score,
                "reference_freq": drift_analysis["reference_freq"]
            }

        except Exception as e:
            logger.debug(f"Doppler analysis error: {e}")
            result.details["error"] = str(e)

        return result

    def _analyze_frequency_drift(self, audio: np.ndarray) -> Dict[str, Any]:
        """Analyze frequency drift across time windows."""
        try:
            # Use short windows to track frequency changes
            window_size = int(0.05 * self.sample_rate)  # 50ms windows
            hop = window_size // 2
            n_windows = (len(audio) - window_size) // hop

            frequencies = []
            for i in range(n_windows):
                start = i * hop
                window = audio[start:start + window_size]

                # Get dominant frequency
                freq = self._estimate_dominant_frequency(window)
                if freq > 0:
                    frequencies.append(freq)

            if len(frequencies) < 3:
                return {
                    "total_drift": 0.0,
                    "micro_movements": 0,
                    "stability": 1.0,
                    "reference_freq": 0.0
                }

            frequencies = np.array(frequencies)
            reference = np.median(frequencies)

            # Total drift
            total_drift = np.max(frequencies) - np.min(frequencies)

            # Count micro-movements (small frequency changes)
            diffs = np.abs(np.diff(frequencies))
            micro_threshold = self.config.NATURAL_MOVEMENT_HZ
            micro_movements = np.sum((diffs > 0.5) & (diffs < micro_threshold * 2))

            # Stability (inverse of coefficient of variation)
            cv = np.std(frequencies) / (reference + 1e-10)
            stability = max(0, 1 - cv * 10)

            return {
                "total_drift": float(total_drift),
                "micro_movements": int(micro_movements),
                "stability": float(stability),
                "reference_freq": float(reference),
                "frequency_series": frequencies.tolist()
            }

        except Exception:
            return {
                "total_drift": 0.0,
                "micro_movements": 0,
                "stability": 1.0,
                "reference_freq": 0.0
            }

    def _estimate_dominant_frequency(self, window: np.ndarray) -> float:
        """Estimate dominant frequency in a window using autocorrelation."""
        try:
            min_period = int(self.sample_rate / 400)
            max_period = int(self.sample_rate / 75)

            autocorr = np.correlate(window, window, mode='full')
            autocorr = autocorr[len(autocorr)//2:]

            if max_period < len(autocorr):
                search = autocorr[min_period:max_period]
                if len(search) > 0:
                    peak = np.argmax(search) + min_period
                    return float(self.sample_rate / peak)

            return 0.0

        except Exception:
            return 0.0

    def _classify_movement_pattern(self, drift_analysis: Dict[str, Any]) -> str:
        """Classify the movement pattern based on drift analysis."""
        drift = drift_analysis["total_drift"]
        micro = drift_analysis["micro_movements"]
        stability = drift_analysis["stability"]

        if drift < 0.5 and micro < 2:
            return "none"  # Static (possible recording)
        elif drift < self.config.MAX_DOPPLER_SHIFT_HZ and micro >= 2:
            return "natural"  # Natural speaking movement
        elif drift < self.config.MAX_DOPPLER_SHIFT_HZ / 2:
            return "subtle"  # Subtle movement (still likely live)
        elif drift > self.config.MAX_DOPPLER_SHIFT_HZ * 2:
            return "erratic"  # Erratic (possible manipulation)
        else:
            return "moderate"


class BayesianConfidenceFusion:
    """
    Bayesian confidence fusion for combining physics-aware and ML features.

    Mathematical Foundation:
    P(authentic|evidence) = P(evidence|authentic) * P(authentic) / P(evidence)

    Evidence combines:
    - ML embedding confidence (ECAPA-TDNN similarity)
    - Physics verification (VTL, reverb, Doppler)
    - Behavioral patterns
    - Environmental context
    """

    def __init__(self):
        self.config = PhysicsConfig()

        # Likelihood distributions (learned from data)
        self._likelihood_authentic: Dict[str, float] = {}
        self._likelihood_spoof: Dict[str, float] = {}

    async def fuse_confidence_async(
        self,
        ml_confidence: float,
        physics_features: PhysicsAwareFeatures,
        behavioral_confidence: Optional[float] = None,
        context_confidence: Optional[float] = None
    ) -> Tuple[float, float, Dict[str, Any]]:
        """Async Bayesian fusion."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.fuse_confidence,
            ml_confidence,
            physics_features,
            behavioral_confidence,
            context_confidence
        )

    def fuse_confidence(
        self,
        ml_confidence: float,
        physics_features: PhysicsAwareFeatures,
        behavioral_confidence: Optional[float] = None,
        context_confidence: Optional[float] = None
    ) -> Tuple[float, float, Dict[str, Any]]:
        """
        Combine multiple evidence sources using Bayesian inference.

        Returns:
            Tuple of (authentic_probability, spoof_probability, details)
        """
        details = {"evidence_weights": {}}

        try:
            # Prior probabilities
            p_auth = self.config.PRIOR_AUTHENTIC
            p_spoof = self.config.PRIOR_SPOOF

            # Calculate likelihoods for each evidence source

            # 1. ML embedding confidence
            ml_auth, ml_spoof = self._ml_likelihood(ml_confidence)
            details["evidence_weights"]["ml"] = {"authentic": ml_auth, "spoof": ml_spoof}

            # 2. Physics features
            physics_auth, physics_spoof = self._physics_likelihood(physics_features)
            details["evidence_weights"]["physics"] = {"authentic": physics_auth, "spoof": physics_spoof}

            # 3. Behavioral confidence (optional)
            if behavioral_confidence is not None:
                behav_auth, behav_spoof = self._behavioral_likelihood(behavioral_confidence)
                details["evidence_weights"]["behavioral"] = {"authentic": behav_auth, "spoof": behav_spoof}
            else:
                behav_auth, behav_spoof = 1.0, 1.0

            # 4. Context confidence (optional)
            if context_confidence is not None:
                ctx_auth, ctx_spoof = self._context_likelihood(context_confidence)
                details["evidence_weights"]["context"] = {"authentic": ctx_auth, "spoof": ctx_spoof}
            else:
                ctx_auth, ctx_spoof = 1.0, 1.0

            # Combine likelihoods (product of independent evidence)
            total_auth = ml_auth * physics_auth * behav_auth * ctx_auth
            total_spoof = ml_spoof * physics_spoof * behav_spoof * ctx_spoof

            # Bayes' theorem
            evidence = (total_auth * p_auth) + (total_spoof * p_spoof)

            if evidence > 0:
                p_auth_given_evidence = (total_auth * p_auth) / evidence
                p_spoof_given_evidence = (total_spoof * p_spoof) / evidence
            else:
                p_auth_given_evidence = p_auth
                p_spoof_given_evidence = p_spoof

            # Normalize to sum to 1
            total = p_auth_given_evidence + p_spoof_given_evidence
            if total > 0:
                p_auth_given_evidence /= total
                p_spoof_given_evidence /= total

            details["prior_authentic"] = p_auth
            details["prior_spoof"] = p_spoof
            details["combined_likelihood_authentic"] = total_auth
            details["combined_likelihood_spoof"] = total_spoof
            details["posterior_authentic"] = p_auth_given_evidence
            details["posterior_spoof"] = p_spoof_given_evidence

            return float(p_auth_given_evidence), float(p_spoof_given_evidence), details

        except Exception as e:
            logger.debug(f"Bayesian fusion error: {e}")
            details["error"] = str(e)
            return self.config.PRIOR_AUTHENTIC, self.config.PRIOR_SPOOF, details

    def _ml_likelihood(self, confidence: float) -> Tuple[float, float]:
        """
        Likelihood of ML confidence given authentic/spoof.

        P(confidence|authentic) modeled as beta distribution
        Authentic: high confidence more likely
        Spoof: lower confidence more likely
        """
        # Authentic speakers typically have higher ML confidence
        # Model: likelihood increases with confidence for authentic
        auth_likelihood = confidence ** 2  # Quadratic increase

        # Spoof attempts typically have lower/moderate confidence
        # Model: inverse relationship
        spoof_likelihood = (1 - confidence) ** 0.5 + 0.1

        return float(auth_likelihood), float(spoof_likelihood)

    def _physics_likelihood(self, features: PhysicsAwareFeatures) -> Tuple[float, float]:
        """
        Likelihood based on physics verification.

        Strong physics evidence heavily influences the posterior.
        """
        physics_score = features.physics_confidence

        # Physics anomalies
        anomaly_penalty = len(features.anomalies_detected) * 0.1

        # Individual component checks
        vtl_ok = features.vocal_tract.is_within_human_range and \
                 features.vocal_tract.is_consistent_with_baseline
        reverb_ok = not features.reverb_analysis.double_reverb_detected and \
                    features.reverb_analysis.is_consistent_with_baseline
        doppler_ok = features.doppler.is_natural_movement

        # Calculate likelihoods
        if vtl_ok and reverb_ok and doppler_ok:
            auth_likelihood = 0.95 - anomaly_penalty
            spoof_likelihood = 0.1
        elif (vtl_ok and reverb_ok) or (vtl_ok and doppler_ok) or (reverb_ok and doppler_ok):
            auth_likelihood = 0.7 - anomaly_penalty
            spoof_likelihood = 0.3
        elif vtl_ok or reverb_ok or doppler_ok:
            auth_likelihood = 0.5 - anomaly_penalty
            spoof_likelihood = 0.5
        else:
            auth_likelihood = 0.2 - anomaly_penalty
            spoof_likelihood = 0.9

        # Double-reverb is strong spoof indicator
        if features.reverb_analysis.double_reverb_detected:
            auth_likelihood *= 0.3
            spoof_likelihood *= 2.0

        # VTL outside human range is strong spoof indicator
        if not features.vocal_tract.is_within_human_range:
            auth_likelihood *= 0.2
            spoof_likelihood *= 3.0

        return max(0.01, float(auth_likelihood)), max(0.01, float(spoof_likelihood))

    def _behavioral_likelihood(self, confidence: float) -> Tuple[float, float]:
        """Likelihood based on behavioral patterns."""
        auth_likelihood = 0.5 + 0.5 * confidence
        spoof_likelihood = 0.5 + 0.5 * (1 - confidence)
        return float(auth_likelihood), float(spoof_likelihood)

    def _context_likelihood(self, confidence: float) -> Tuple[float, float]:
        """Likelihood based on context (time, location, etc.)."""
        auth_likelihood = 0.5 + 0.5 * confidence
        spoof_likelihood = 0.5 + 0.5 * (1 - confidence)
        return float(auth_likelihood), float(spoof_likelihood)


class PhysicsAwareFeatureExtractor:
    """
    Complete physics-aware feature extraction orchestrator.

    Combines:
    - Traditional voice features (MFCC, spectral, prosodic)
    - Reverberation analysis (RT60, double-reverb)
    - Vocal tract modeling (VTL from formants)
    - Doppler effect analysis
    - Bayesian confidence fusion
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate

        # Initialize analyzers
        self._reverb_analyzer = ReverbAnalyzer(sample_rate)
        self._vtl_analyzer = VocalTractAnalyzer(sample_rate)
        self._doppler_analyzer = DopplerAnalyzer(sample_rate)
        self._bayesian_fusion = BayesianConfidenceFusion()

        # Traditional feature extractor
        self._voice_extractor = VoiceFeatureExtractor(sample_rate)

        # Baseline storage
        self._baseline_vtl: Optional[float] = None
        self._baseline_rt60: Optional[float] = None

        logger.info("✅ PhysicsAwareFeatureExtractor initialized")

    async def extract_physics_features_async(
        self,
        audio_data: bytes,
        ml_confidence: Optional[float] = None,
        behavioral_confidence: Optional[float] = None,
        context_confidence: Optional[float] = None
    ) -> PhysicsAwareFeatures:
        """
        Async comprehensive physics-aware feature extraction.

        Runs all analyzers in parallel for efficiency.
        """
        import time
        start_time = time.time()

        result = PhysicsAwareFeatures()

        try:
            # Run all physics analyzers in parallel
            reverb_task = self._reverb_analyzer.analyze_reverberation_async(
                audio_data, self._baseline_rt60
            )
            vtl_task = self._vtl_analyzer.analyze_vocal_tract_async(
                audio_data, self._baseline_vtl
            )
            doppler_task = self._doppler_analyzer.analyze_doppler_async(audio_data)

            # Await all
            reverb_result, vtl_result, doppler_result = await asyncio.gather(
                reverb_task, vtl_task, doppler_task
            )

            result.reverb_analysis = reverb_result
            result.vocal_tract = vtl_result
            result.doppler = doppler_result

            # Calculate physics confidence score
            result.physics_scores = self._calculate_physics_scores(result)
            result.physics_confidence = self._aggregate_physics_confidence(result.physics_scores)

            # Determine confidence level
            result.physics_level = self._determine_confidence_level(
                result.physics_confidence, result
            )

            # Detect anomalies
            result.anomalies_detected = self._detect_anomalies(result)

            # Bayesian fusion if ML confidence provided
            if ml_confidence is not None:
                auth_prob, spoof_prob, fusion_details = await self._bayesian_fusion.fuse_confidence_async(
                    ml_confidence, result, behavioral_confidence, context_confidence
                )
                result.bayesian_authentic_probability = auth_prob
                result.bayesian_spoof_probability = spoof_prob

            # Update baselines with good samples
            if result.physics_confidence > 0.7:
                if result.vocal_tract.vtl_estimated_cm > 0:
                    self._vtl_analyzer.update_baseline(result.vocal_tract.vtl_estimated_cm)
                    self._baseline_vtl = self._vtl_analyzer._baseline_vtl

                if result.reverb_analysis.rt60_estimated > 0:
                    self._reverb_analyzer.update_baseline(result.reverb_analysis.rt60_estimated)
                    self._baseline_rt60 = self._reverb_analyzer._baseline_rt60

        except Exception as e:
            logger.error(f"Physics feature extraction error: {e}")
            result.anomalies_detected.append(f"extraction_error: {str(e)}")

        result.extraction_time_ms = (time.time() - start_time) * 1000
        result.timestamp = datetime.utcnow()

        return result

    def extract_physics_features(
        self,
        audio_data: bytes,
        ml_confidence: Optional[float] = None,
        behavioral_confidence: Optional[float] = None,
        context_confidence: Optional[float] = None
    ) -> PhysicsAwareFeatures:
        """Synchronous wrapper for physics feature extraction."""
        return asyncio.run(self.extract_physics_features_async(
            audio_data, ml_confidence, behavioral_confidence, context_confidence
        ))

    def _calculate_physics_scores(self, features: PhysicsAwareFeatures) -> Dict[str, float]:
        """Calculate individual physics component scores."""
        scores = {}

        # Reverb score
        reverb = features.reverb_analysis
        scores["reverb"] = 1.0 if not reverb.double_reverb_detected else (1.0 - reverb.double_reverb_confidence)
        if reverb.is_consistent_with_baseline:
            scores["reverb"] = min(1.0, scores["reverb"] + 0.1)

        # VTL score
        vtl = features.vocal_tract
        if vtl.vtl_estimated_cm > 0:
            scores["vtl"] = 0.5 if vtl.is_within_human_range else 0.0
            scores["vtl"] += 0.3 * vtl.formant_consistency
            if vtl.is_consistent_with_baseline:
                scores["vtl"] += 0.2
        else:
            scores["vtl"] = 0.5  # Neutral if can't estimate

        # Doppler score
        doppler = features.doppler
        if doppler.is_natural_movement:
            scores["doppler"] = 0.8 + 0.2 * doppler.stability_score
        elif doppler.movement_pattern == "none":
            scores["doppler"] = 0.3  # Static = suspicious but not definitive
        else:
            scores["doppler"] = 0.5

        return scores

    def _aggregate_physics_confidence(self, scores: Dict[str, float]) -> float:
        """Aggregate individual scores into overall physics confidence."""
        if not scores:
            return 0.5

        # Weighted average
        weights = {"reverb": 0.35, "vtl": 0.40, "doppler": 0.25}
        total_weight = sum(weights.get(k, 0) for k in scores.keys())
        weighted_sum = sum(scores[k] * weights.get(k, 0) for k in scores.keys())

        if total_weight > 0:
            return float(weighted_sum / total_weight)
        return 0.5

    def _determine_confidence_level(
        self,
        confidence: float,
        features: PhysicsAwareFeatures
    ) -> PhysicsConfidenceLevel:
        """Determine physics confidence level from score and features."""

        # Check for hard failures
        if features.reverb_analysis.double_reverb_detected and \
           features.reverb_analysis.double_reverb_confidence > 0.8:
            return PhysicsConfidenceLevel.PHYSICS_FAILED

        if not features.vocal_tract.is_within_human_range and \
           features.vocal_tract.vtl_estimated_cm > 0:
            return PhysicsConfidenceLevel.PHYSICS_FAILED

        # Score-based levels
        if confidence >= 0.85:
            return PhysicsConfidenceLevel.PHYSICS_VERIFIED
        elif confidence >= 0.7:
            return PhysicsConfidenceLevel.PHYSICS_LIKELY
        elif confidence >= 0.5:
            return PhysicsConfidenceLevel.PHYSICS_UNCERTAIN
        elif confidence >= 0.3:
            return PhysicsConfidenceLevel.PHYSICS_SUSPICIOUS
        else:
            return PhysicsConfidenceLevel.PHYSICS_FAILED

    def _detect_anomalies(self, features: PhysicsAwareFeatures) -> List[str]:
        """Detect physics anomalies that might indicate spoofing."""
        anomalies = []

        # Double reverb
        if features.reverb_analysis.double_reverb_detected:
            anomalies.append(f"double_reverb_detected (confidence: {features.reverb_analysis.double_reverb_confidence:.2f})")

        # VTL anomalies
        if not features.vocal_tract.is_within_human_range:
            anomalies.append(f"vtl_outside_human_range ({features.vocal_tract.vtl_estimated_cm:.1f} cm)")
        if not features.vocal_tract.is_consistent_with_baseline and features.vocal_tract.vtl_deviation_cm > 0:
            anomalies.append(f"vtl_baseline_deviation ({features.vocal_tract.vtl_deviation_cm:.1f} cm)")

        # Reverb consistency
        if not features.reverb_analysis.is_consistent_with_baseline:
            anomalies.append("reverb_baseline_inconsistent")

        # Doppler anomalies
        if not features.doppler.is_natural_movement:
            anomalies.append(f"unnatural_movement_pattern ({features.doppler.movement_pattern})")

        return anomalies

    def get_combined_features(
        self,
        audio_data: bytes
    ) -> Tuple[VoiceFeatures, PhysicsAwareFeatures]:
        """
        Extract both traditional and physics-aware features.

        Returns combined feature set for comprehensive analysis.
        """
        voice_features = self._voice_extractor.extract_features(audio_data)
        physics_features = self.extract_physics_features(audio_data)
        return voice_features, physics_features

    def get_statistics(self) -> Dict[str, Any]:
        """Get extractor statistics."""
        return {
            "sample_rate": self.sample_rate,
            "baseline_vtl_cm": self._baseline_vtl,
            "baseline_rt60_seconds": self._baseline_rt60,
            "vtl_samples": len(self._vtl_analyzer._vtl_history),
            "config": {
                "speed_of_sound": PhysicsConfig.SPEED_OF_SOUND_MPS,
                "vtl_range_cm": (PhysicsConfig.VTL_MIN_CM, PhysicsConfig.VTL_MAX_CM),
                "rt60_range_s": (PhysicsConfig.RT60_MIN_SECONDS, PhysicsConfig.RT60_MAX_SECONDS)
            }
        }


# =============================================================================
# MODULE-LEVEL ACCESSORS
# =============================================================================

_physics_extractor: Optional[PhysicsAwareFeatureExtractor] = None


def get_physics_feature_extractor(sample_rate: int = 16000) -> PhysicsAwareFeatureExtractor:
    """Get or create the physics-aware feature extractor singleton."""
    global _physics_extractor
    if _physics_extractor is None:
        _physics_extractor = PhysicsAwareFeatureExtractor(sample_rate)
    return _physics_extractor


def reset_physics_extractor():
    """Reset the physics extractor singleton."""
    global _physics_extractor
    _physics_extractor = None
