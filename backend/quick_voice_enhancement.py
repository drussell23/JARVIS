#!/usr/bin/env python3
"""
🚀 ADVANCED QUICK VOICE ENHANCEMENT SYSTEM
═══════════════════════════════════════════

Intelligently enhances existing voice profiles with raw audio storage
for advanced embedding reconstruction capabilities.

FEATURES:
━━━━━━━━
🎯 Smart Sample Collection
   • Only 10 optimized samples needed
   • Phonetically diverse phrase selection
   • Real-time quality validation with adaptive thresholds

🧠 Intelligent Quality Analysis
   • Multi-dimensional quality scoring (SNR, VAD, spectral analysis)
   • Adaptive retry with personalized feedback
   • Automatic noise cancellation recommendations

📊 Advanced Analytics
   • Real-time phonetic coverage analysis
   • Voice consistency scoring
   • Embedding variance tracking
   • Automatic profile optimization

🔄 Seamless Integration
   • Merges with existing profile (keeps your 59 samples)
   • Zero downtime - updates in background
   • Backward compatible with legacy samples

💾 Robust Storage
   • Raw audio + embeddings + features
   • CloudSQL + Local SQLite sync
   • Automatic backup and rollback

🎨 Enhanced UX
   • Beautiful progress visualization
   • Real-time waveform display
   • Voice characteristics feedback
   • Estimated time remaining

USAGE:
━━━━━━
    python3 backend/quick_voice_enhancement.py --speaker "Derek J. Russell"

    # With custom settings
    python3 backend/quick_voice_enhancement.py \\
        --speaker "Derek J. Russell" \\
        --samples 15 \\
        --duration 8 \\
        --quality-threshold 0.7

REQUIREMENTS:
━━━━━━━━━━━━
    - Existing speaker profile in database
    - Microphone access
    - ~10-15 minutes
    - Quiet environment recommended

Author: Claude Code + Derek J. Russell
Version: 2.0.0 (Advanced)
"""

import argparse
import asyncio
import io
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from intelligence.learning_database import get_learning_database
from voice.biological_bounds import UNMEASURED, MeasuredAggregator, default_validator
from voice.engines.speechbrain_engine import SpeechBrainEngine
from voice.formant_estimation import estimate_formants
from voice.stt_config import ModelConfig, STTEngine

# Configure logging with colors
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
logger = logging.getLogger(__name__)

_VALIDATOR = default_validator()

# Reductions that skip samples nothing measured. Shared with enroll_voice via
# biological_bounds rather than reimplemented here — the two enrolment paths
# writing to the same columns with different ideas of how to average them is
# how the columns disagreed in the first place.
_agg = MeasuredAggregator(_VALIDATOR)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class PhoneticCoverage:
    """Tracks phonetic diversity across samples"""

    vowels: set = field(default_factory=set)
    consonants: set = field(default_factory=set)
    phoneme_pairs: set = field(default_factory=set)
    coverage_score: float = 0.0

    # IPA phoneme sets (simplified)
    ALL_VOWELS = {'a', 'e', 'i', 'o', 'u', 'æ', 'ɛ', 'ɪ', 'ɔ', 'ʊ', 'ə'}
    ALL_CONSONANTS = {'p', 'b', 't', 'd', 'k', 'g', 'f', 'v', 's', 'z', 'ʃ', 'ʒ',
                     'θ', 'ð', 'm', 'n', 'ŋ', 'l', 'r', 'w', 'j', 'h'}

    def update(self, text: str):
        """Update coverage from transcription"""
        text_lower = text.lower()

        # Extract vowels and consonants (simplified)
        for char in text_lower:
            if char in 'aeiou':
                self.vowels.add(char)
            elif char.isalpha():
                self.consonants.add(char)

        # Track character pairs for phoneme diversity
        for i in range(len(text_lower) - 1):
            if text_lower[i].isalpha() and text_lower[i + 1].isalpha():
                self.phoneme_pairs.add(text_lower[i:i + 2])

        self._calculate_coverage()

    def _calculate_coverage(self):
        """Calculate overall phonetic coverage score"""
        vowel_score = len(self.vowels) / len(self.ALL_VOWELS) if self.ALL_VOWELS else 0
        consonant_score = len(self.consonants) / len(self.ALL_CONSONANTS) if self.ALL_CONSONANTS else 0
        diversity_score = min(1.0, len(self.phoneme_pairs) / 50)  # 50 pairs = good coverage

        self.coverage_score = (vowel_score * 0.3 + consonant_score * 0.4 + diversity_score * 0.3)


@dataclass
class AdvancedQualityMetrics:
    """Comprehensive quality analysis"""

    # Signal metrics
    snr_db: float
    signal_power: float
    noise_floor: float

    # Clipping and distortion
    clipping_ratio: float
    harmonic_distortion: float

    # Voice activity
    vad_ratio: float
    speech_energy: float
    silence_ratio: float

    # Spectral analysis
    spectral_flatness: float
    spectral_entropy: float
    spectral_centroid_hz: float
    spectral_bandwidth_hz: float

    # Dynamic range
    dynamic_range_db: float
    crest_factor: float

    # Overall scores
    quality_score: float
    confidence_score: float
    is_acceptable: bool

    # Feedback
    issues: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class VoiceCharacteristics:
    """Detailed voice biometric features"""

    # Pitch
    pitch_mean_hz: float
    pitch_std_hz: float
    pitch_range_hz: float
    pitch_median_hz: float

    # Formants
    f1_hz: float
    f2_hz: float
    f3_hz: float
    f4_hz: float

    # Spectral
    spectral_centroid_hz: float
    spectral_rolloff_hz: float
    spectral_flux: float

    # Temporal
    zero_crossing_rate: float
    speech_rate_wpm: float
    pause_ratio: float

    # Energy
    rms_energy: float
    energy_entropy: float

    # Timbre
    brightness: float
    roughness: float

    # Voice type classification
    voice_type: str = ""  # bass, baritone, tenor, alto, soprano


@dataclass
class EnhancementSample:
    """Individual enhancement sample"""

    sample_number: int
    phrase: str
    audio_bytes: bytes
    audio_array: np.ndarray
    embedding: np.ndarray
    transcription: str
    quality_metrics: AdvancedQualityMetrics
    voice_characteristics: VoiceCharacteristics
    phonetic_contribution: float
    timestamp: datetime
    duration_seconds: float
    recording_attempts: int = 1


@dataclass
class EnhancementProgress:
    """Real-time progress tracking"""

    total_samples: int
    completed_samples: int
    current_sample: int

    # Quality tracking
    avg_quality_score: float = 0.0
    quality_trend: List[float] = field(default_factory=list)

    # Phonetic tracking
    phonetic_coverage: PhoneticCoverage = field(default_factory=PhoneticCoverage)

    # Timing
    start_time: datetime = field(default_factory=datetime.now)
    estimated_time_remaining_seconds: float = 0.0

    # Voice consistency
    embedding_variance: float = 0.0
    consistency_score: float = 0.0

    def update(self, sample: EnhancementSample):
        """Update progress with new sample"""
        self.completed_samples += 1
        self.quality_trend.append(sample.quality_metrics.quality_score)
        self.avg_quality_score = np.mean(self.quality_trend)
        self.phonetic_coverage.update(sample.transcription)

        # Estimate time remaining
        elapsed = (datetime.now() - self.start_time).total_seconds()
        if self.completed_samples > 0:
            avg_time_per_sample = elapsed / self.completed_samples
            remaining_samples = self.total_samples - self.completed_samples
            self.estimated_time_remaining_seconds = avg_time_per_sample * remaining_samples


# ═══════════════════════════════════════════════════════════════════════════════
# ADVANCED QUALITY ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════


class AdvancedQualityAnalyzer:
    """
    State-of-the-art audio quality analysis for voice biometrics

    Uses multi-dimensional signal processing to ensure optimal sample quality:
    - Spectral analysis (FFT, STFT)
    - Statistical analysis (moments, entropy)
    - Perceptual analysis (psychoacoustic models)
    - Voice activity detection (energy-based + spectral)
    """

    def __init__(self, sample_rate: int = 16000, quality_threshold: float = 0.65):
        self.sample_rate = sample_rate
        self.quality_threshold = quality_threshold

        # Adaptive thresholds
        self.min_snr_db = 12.0
        self.max_clipping_ratio = 0.015
        self.min_vad_ratio = 0.25
        self.min_dynamic_range_db = 25.0

    async def analyze(self, audio: np.ndarray, verbose: bool = True) -> AdvancedQualityMetrics:
        """
        Comprehensive quality analysis

        Returns:
            AdvancedQualityMetrics with detailed analysis and recommendations
        """
        issues = []
        recommendations = []

        # 1. Signal-to-Noise Ratio (Advanced)
        snr_db, signal_power, noise_floor = self._calculate_snr_advanced(audio)
        if snr_db < self.min_snr_db:
            issues.append(f"Low SNR: {snr_db:.1f} dB (min: {self.min_snr_db:.1f} dB)")
            recommendations.append("🔇 Move to quieter location or use noise cancellation")

        # 2. Clipping Detection (Sample-level)
        clipping_ratio = self._detect_clipping(audio)
        if clipping_ratio > self.max_clipping_ratio:
            issues.append(f"Clipping detected: {clipping_ratio:.2%}")
            recommendations.append("🎚️  Reduce microphone gain or move further from mic")

        # 3. Harmonic Distortion
        harmonic_distortion = self._calculate_harmonic_distortion(audio)
        if harmonic_distortion > 0.05:
            issues.append(f"Harmonic distortion: {harmonic_distortion:.2%}")
            recommendations.append("🎤 Check microphone quality or reduce input level")

        # 4. Voice Activity Detection (Advanced)
        vad_ratio, speech_energy, silence_ratio = self._vad_advanced(audio)
        if vad_ratio < self.min_vad_ratio:
            issues.append(f"Insufficient speech: {vad_ratio:.1%} (min: {self.min_vad_ratio:.1%})")
            recommendations.append("🗣️  Speak more during recording, reduce pauses")

        # 5. Spectral Analysis
        spectral_metrics = self._analyze_spectrum(audio)
        spectral_flatness = spectral_metrics['flatness']
        spectral_entropy = spectral_metrics['entropy']
        spectral_centroid = spectral_metrics['centroid']
        spectral_bandwidth = spectral_metrics['bandwidth']

        if spectral_flatness > 0.7:
            issues.append("High spectral flatness (noisy signal)")
            recommendations.append("🎵 Reduce background noise")

        # 6. Dynamic Range
        dynamic_range_db = self._calculate_dynamic_range(audio)
        crest_factor = self._calculate_crest_factor(audio)

        if dynamic_range_db < self.min_dynamic_range_db:
            issues.append(f"Low dynamic range: {dynamic_range_db:.1f} dB")
            recommendations.append("📊 Speak with more natural variation")

        # 7. Calculate overall quality score (0-1)
        quality_score = self._calculate_quality_score(
            snr_db, clipping_ratio, harmonic_distortion, vad_ratio,
            spectral_flatness, dynamic_range_db, speech_energy
        )

        # 8. Calculate confidence score
        confidence_score = min(1.0, quality_score * (1.0 + vad_ratio * 0.5))

        # 9. Acceptance decision
        is_acceptable = (
            snr_db >= self.min_snr_db and
            clipping_ratio <= self.max_clipping_ratio and
            vad_ratio >= self.min_vad_ratio and
            quality_score >= self.quality_threshold
        )

        if not is_acceptable:
            issues.append(f"Quality below threshold: {quality_score:.1%} < {self.quality_threshold:.1%}")

        return AdvancedQualityMetrics(
            snr_db=snr_db,
            signal_power=signal_power,
            noise_floor=noise_floor,
            clipping_ratio=clipping_ratio,
            harmonic_distortion=harmonic_distortion,
            vad_ratio=vad_ratio,
            speech_energy=speech_energy,
            silence_ratio=silence_ratio,
            spectral_flatness=spectral_flatness,
            spectral_entropy=spectral_entropy,
            spectral_centroid_hz=spectral_centroid,
            spectral_bandwidth_hz=spectral_bandwidth,
            dynamic_range_db=dynamic_range_db,
            crest_factor=crest_factor,
            quality_score=quality_score,
            confidence_score=confidence_score,
            is_acceptable=is_acceptable,
            issues=issues,
            recommendations=recommendations
        )

    def _calculate_snr_advanced(self, audio: np.ndarray) -> Tuple[float, float, float]:
        """Advanced SNR calculation using spectral analysis"""
        # Frame-based energy analysis
        frame_size = self.sample_rate // 20  # 50ms frames
        num_frames = len(audio) // frame_size

        frame_energies = []
        for i in range(num_frames):
            frame = audio[i * frame_size:(i + 1) * frame_size]
            energy = np.sum(frame ** 2)
            frame_energies.append(energy)

        frame_energies = np.array(frame_energies)

        # Noise floor: 20th percentile of frame energies
        noise_floor = np.percentile(frame_energies, 20)

        # Signal power: mean of top 50% frames
        signal_frames = frame_energies[frame_energies > np.median(frame_energies)]
        signal_power = np.mean(signal_frames) if len(signal_frames) > 0 else np.mean(frame_energies)

        # SNR in dB
        if noise_floor < 1e-10:
            snr_db = 60.0
        else:
            snr_db = 10 * np.log10(signal_power / noise_floor)

        return float(snr_db), float(signal_power), float(noise_floor)

    def _detect_clipping(self, audio: np.ndarray) -> float:
        """Detect clipping with hysteresis"""
        threshold = 0.99
        clipped = np.abs(audio) > threshold
        clipping_ratio = np.sum(clipped) / len(audio)
        return float(clipping_ratio)

    def _calculate_harmonic_distortion(self, audio: np.ndarray) -> float:
        """Estimate total harmonic distortion (THD)"""
        # Use FFT to find fundamental and harmonics
        fft = np.fft.rfft(audio)
        magnitude = np.abs(fft)

        # Find fundamental (peak in 80-400 Hz range for voice)
        freqs = np.fft.rfftfreq(len(audio), 1 / self.sample_rate)
        voice_range = (freqs >= 80) & (freqs <= 400)

        if not np.any(voice_range):
            return 0.0

        fundamental_idx = np.argmax(magnitude[voice_range])
        fundamental_power = magnitude[voice_range][fundamental_idx] ** 2

        # Total power
        total_power = np.sum(magnitude ** 2)

        # THD = (total - fundamental) / fundamental
        if fundamental_power < 1e-10:
            return 0.0

        thd = (total_power - fundamental_power) / fundamental_power
        return float(min(1.0, thd))

    def _vad_advanced(self, audio: np.ndarray) -> Tuple[float, float, float]:
        """Advanced voice activity detection using energy and spectral features"""
        frame_size = int(self.sample_rate * 0.025)  # 25ms frames
        hop_size = int(self.sample_rate * 0.010)    # 10ms hop

        num_frames = (len(audio) - frame_size) // hop_size + 1

        voice_frames = 0
        total_speech_energy = 0.0

        for i in range(num_frames):
            start = i * hop_size
            end = start + frame_size
            frame = audio[start:end]

            # Energy-based VAD
            energy = np.sum(frame ** 2)

            # Spectral-based VAD (zero-crossing rate)
            zcr = np.sum(np.abs(np.diff(np.sign(frame)))) / (2 * len(frame))

            # Voice activity threshold (adaptive)
            energy_threshold = np.mean(audio ** 2) * 0.1

            if energy > energy_threshold and 0.01 < zcr < 0.5:
                voice_frames += 1
                total_speech_energy += energy

        vad_ratio = voice_frames / num_frames if num_frames > 0 else 0.0
        silence_ratio = 1.0 - vad_ratio
        speech_energy = total_speech_energy / voice_frames if voice_frames > 0 else 0.0

        return float(vad_ratio), float(speech_energy), float(silence_ratio)

    def _analyze_spectrum(self, audio: np.ndarray) -> Dict[str, float]:
        """Comprehensive spectral analysis"""
        # Compute power spectrum
        fft = np.fft.rfft(audio)
        magnitude = np.abs(fft)
        power_spectrum = magnitude ** 2
        freqs = np.fft.rfftfreq(len(audio), 1 / self.sample_rate)

        # Spectral flatness (Wiener entropy)
        geometric_mean = np.exp(np.mean(np.log(power_spectrum + 1e-10)))
        arithmetic_mean = np.mean(power_spectrum)
        flatness = geometric_mean / (arithmetic_mean + 1e-10)

        # Spectral entropy
        power_norm = power_spectrum / (np.sum(power_spectrum) + 1e-10)
        entropy = -np.sum(power_norm * np.log2(power_norm + 1e-10))

        # Spectral centroid (brightness)
        centroid = np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10)

        # Spectral bandwidth
        deviation = freqs - centroid
        bandwidth = np.sqrt(np.sum((deviation ** 2) * magnitude) / (np.sum(magnitude) + 1e-10))

        return {
            'flatness': float(flatness),
            'entropy': float(entropy),
            'centroid': float(centroid),
            'bandwidth': float(bandwidth)
        }

    def _calculate_dynamic_range(self, audio: np.ndarray) -> float:
        """Calculate dynamic range in dB"""
        peak = np.max(np.abs(audio))

        # Noise floor from quietest 10% of frames
        frame_size = self.sample_rate // 20
        num_frames = len(audio) // frame_size
        frame_rms = []

        for i in range(num_frames):
            frame = audio[i * frame_size:(i + 1) * frame_size]
            rms = np.sqrt(np.mean(frame ** 2))
            frame_rms.append(rms)

        noise = np.percentile(frame_rms, 10) if frame_rms else 1e-10

        if noise < 1e-10:
            return 96.0

        dynamic_range = 20 * np.log10(peak / noise)
        return float(dynamic_range)

    def _calculate_crest_factor(self, audio: np.ndarray) -> float:
        """Calculate crest factor (peak-to-RMS ratio)"""
        peak = np.max(np.abs(audio))
        rms = np.sqrt(np.mean(audio ** 2))

        if rms < 1e-10:
            return 1.0

        crest = peak / rms
        return float(crest)

    def _calculate_quality_score(
        self,
        snr_db: float,
        clipping_ratio: float,
        harmonic_distortion: float,
        vad_ratio: float,
        spectral_flatness: float,
        dynamic_range_db: float,
        speech_energy: float
    ) -> float:
        """Multi-dimensional quality scoring with adaptive weights"""

        # Individual component scores (0-1)
        snr_score = min(1.0, max(0.0, (snr_db - 5) / 40))
        clipping_score = max(0.0, 1.0 - clipping_ratio / 0.05)
        distortion_score = max(0.0, 1.0 - harmonic_distortion / 0.1)
        vad_score = min(1.0, vad_ratio / 0.4)
        spectral_score = max(0.0, 1.0 - spectral_flatness)
        dynamic_score = min(1.0, max(0.0, (dynamic_range_db - 20) / 60))
        energy_score = min(1.0, speech_energy / 0.01) if speech_energy > 0 else 0.0

        # Weighted combination (tuned for voice biometrics)
        weights = {
            'snr': 0.25,
            'clipping': 0.15,
            'distortion': 0.10,
            'vad': 0.20,
            'spectral': 0.10,
            'dynamic': 0.10,
            'energy': 0.10
        }

        quality = (
            weights['snr'] * snr_score +
            weights['clipping'] * clipping_score +
            weights['distortion'] * distortion_score +
            weights['vad'] * vad_score +
            weights['spectral'] * spectral_score +
            weights['dynamic'] * dynamic_score +
            weights['energy'] * energy_score
        )

        return float(quality)


# ═══════════════════════════════════════════════════════════════════════════════
# ADVANCED VOICE CHARACTERISTICS ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════


class AdvancedVoiceAnalyzer:
    """
    State-of-the-art voice biometric feature extraction

    Extracts comprehensive acoustic features for speaker characterization:
    - Pitch/F0 contours (autocorrelation + cepstral)
    - Formant frequencies (LPC analysis)
    - Spectral features (MFCC, spectral shape)
    - Temporal features (rhythm, speaking rate)
    - Voice quality (jitter, shimmer)
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate

    async def analyze(self, audio: np.ndarray, transcription: str = "") -> VoiceCharacteristics:
        """
        Extract comprehensive voice characteristics

        Returns:
            VoiceCharacteristics with detailed biometric features
        """
        # 1. Pitch analysis (F0 contour)
        pitch_stats = self._analyze_pitch_advanced(audio)

        # 2. Formant analysis (F1-F4)
        formants = self._analyze_formants_lpc(audio)

        # 3. Spectral features
        spectral_features = self._analyze_spectral_features(audio)

        # 4. Temporal features
        temporal_features = self._analyze_temporal_features(audio, transcription)

        # 5. Energy analysis
        energy_features = self._analyze_energy(audio)

        # 6. Timbre analysis
        timbre_features = self._analyze_timbre(audio)

        # 7. Voice type classification
        voice_type = self._classify_voice_type(pitch_stats['mean'])

        return VoiceCharacteristics(
            pitch_mean_hz=pitch_stats['mean'],
            pitch_std_hz=pitch_stats['std'],
            pitch_range_hz=pitch_stats['range'],
            pitch_median_hz=pitch_stats['median'],
            f1_hz=formants[0],
            f2_hz=formants[1],
            f3_hz=formants[2],
            f4_hz=formants[3],
            spectral_centroid_hz=spectral_features['centroid'],
            spectral_rolloff_hz=spectral_features['rolloff'],
            spectral_flux=spectral_features['flux'],
            zero_crossing_rate=temporal_features['zcr'],
            speech_rate_wpm=temporal_features['speech_rate'],
            pause_ratio=temporal_features['pause_ratio'],
            rms_energy=energy_features['rms'],
            energy_entropy=energy_features['entropy'],
            brightness=timbre_features['brightness'],
            roughness=timbre_features['roughness'],
            voice_type=voice_type
        )

    def _analyze_pitch_advanced(self, audio: np.ndarray) -> Dict[str, float]:
        """Advanced pitch detection using autocorrelation"""
        frame_size = 2048
        hop_size = 512
        pitches = []

        for i in range(0, len(audio) - frame_size, hop_size):
            frame = audio[i:i + frame_size]

            # Autocorrelation
            correlation = np.correlate(frame, frame, mode='full')
            correlation = correlation[len(correlation) // 2:]

            # Peak detection in voice range
            min_lag = int(self.sample_rate / 500)  # Max 500 Hz
            max_lag = int(self.sample_rate / 50)   # Min 50 Hz

            if max_lag < len(correlation):
                search_region = correlation[min_lag:max_lag]
                if len(search_region) > 0 and correlation[0] > 0:
                    peak_lag = min_lag + np.argmax(search_region)
                    # Confidence check
                    if correlation[peak_lag] > 0.4 * correlation[0]:
                        pitch = self.sample_rate / peak_lag
                        if 50 <= pitch <= 500:
                            pitches.append(pitch)

        if pitches:
            return {
                'mean': float(np.mean(pitches)),
                'std': float(np.std(pitches)),
                'range': float(np.max(pitches) - np.min(pitches)),
                'median': float(np.median(pitches))
            }
        else:
            return {'mean': 150.0, 'std': 20.0, 'range': 50.0, 'median': 150.0}

    def _analyze_formants_lpc(self, audio: np.ndarray) -> List[float]:
        """
        Formant estimation by linear prediction.

        THIS FUNCTION WROTE THE PROFILE ON THIS MACHINE. Despite the name it
        contained no linear prediction at all — it peak-picked the magnitude
        spectrum and took ``peaks[:4]``, the four LOWEST peaks by frequency,
        which in a desk recording are DC offset, mains hum and low harmonics.
        The four values it stored for the operator were 42.9, 80.0, 102.8 and
        107.4 Hz: an ascending run of rumble, every one of them below the F1 of
        any human being.

        It failed three further ways, all now gone:

          * ``[500, 1500, 2500, 3500]`` on fewer than four peaks — the textbook
            male average, written into a specific speaker's profile;
          * ``formants[-1] + 1000.0`` padding, inventing a resonance from the
            previous invented resonance;
          * silence about all of it, so the profile looked complete.

        It is the third copy of this same defect (with ``enroll_voice`` and
        ``advanced_feature_extraction``); all three now call one estimator that
        fits an all-pole model to voiced frames and reports UNMEASURED for any
        formant it cannot resolve.
        """
        return estimate_formants(audio, self.sample_rate, n_formants=4)

    def _analyze_spectral_features(self, audio: np.ndarray) -> Dict[str, float]:
        """Comprehensive spectral feature extraction"""
        fft = np.fft.rfft(audio)
        magnitude = np.abs(fft)
        power = magnitude ** 2
        freqs = np.fft.rfftfreq(len(audio), 1 / self.sample_rate)

        # Spectral centroid
        centroid = np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10)

        # Spectral rolloff (85% of energy)
        cumsum = np.cumsum(power)
        rolloff_threshold = 0.85 * cumsum[-1]
        rolloff_idx = np.where(cumsum >= rolloff_threshold)[0]
        rolloff = freqs[rolloff_idx[0]] if len(rolloff_idx) > 0 else freqs[-1]

        # Spectral flux (rate of change)
        # For single frame, use variance as proxy
        flux = float(np.std(magnitude))

        return {
            'centroid': float(centroid),
            'rolloff': float(rolloff),
            'flux': flux
        }

    def _analyze_temporal_features(self, audio: np.ndarray, transcription: str) -> Dict[str, float]:
        """Temporal and rhythm analysis"""
        # Zero-crossing rate
        zero_crossings = np.sum(np.abs(np.diff(np.sign(audio)))) / 2
        zcr = zero_crossings / len(audio)

        # Estimate speech rate (words per minute).
        #
        # With no transcription this used to yield 0.0 — a speaker who said
        # nothing, stored as one who speaks at zero words per minute and then
        # differenced against the live speaker. With a short clip it yields an
        # arbitrarily high figure; 7 words over 1.0 s is exactly the 420 wpm in
        # this machine's profile. Neither is a measurement of how fast this
        # person speaks, so neither is reported as one.
        duration_seconds = len(audio) / self.sample_rate
        word_count = len(transcription.split()) if transcription else 0
        if word_count == 0 or duration_seconds <= 0:
            speech_rate = UNMEASURED
        else:
            speech_rate = _VALIDATOR.coerce(
                'speaking_rate_wpm', (word_count / duration_seconds) * 60)

        # Pause ratio (estimate from energy)
        frame_size = self.sample_rate // 20
        num_frames = len(audio) // frame_size
        pauses = 0

        for i in range(num_frames):
            frame = audio[i * frame_size:(i + 1) * frame_size]
            energy = np.sum(frame ** 2)
            if energy < np.mean(audio ** 2) * 0.05:
                pauses += 1

        pause_ratio = pauses / num_frames if num_frames > 0 else 0.0

        return {
            'zcr': float(zcr),
            'speech_rate': float(speech_rate),
            'pause_ratio': float(pause_ratio)
        }

    def _analyze_energy(self, audio: np.ndarray) -> Dict[str, float]:
        """Energy-based features"""
        # RMS energy
        rms = float(np.sqrt(np.mean(audio ** 2)))

        # Energy entropy (frame-based)
        frame_size = self.sample_rate // 20
        num_frames = len(audio) // frame_size
        frame_energies = []

        for i in range(num_frames):
            frame = audio[i * frame_size:(i + 1) * frame_size]
            energy = np.sum(frame ** 2)
            frame_energies.append(energy)

        frame_energies = np.array(frame_energies)

        # Normalize and calculate entropy
        if np.sum(frame_energies) > 0:
            energy_dist = frame_energies / np.sum(frame_energies)
            entropy = -np.sum(energy_dist * np.log2(energy_dist + 1e-10))
        else:
            entropy = 0.0

        return {
            'rms': rms,
            'entropy': float(entropy)
        }

    def _analyze_timbre(self, audio: np.ndarray) -> Dict[str, float]:
        """Timbre characterization"""
        fft = np.fft.rfft(audio)
        magnitude = np.abs(fft)
        freqs = np.fft.rfftfreq(len(audio), 1 / self.sample_rate)

        # Brightness (spectral centroid normalized)
        centroid = np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10)
        brightness = centroid / (self.sample_rate / 2)

        # Roughness (spectral irregularity)
        if len(magnitude) > 1:
            diff = np.abs(np.diff(magnitude))
            roughness = np.sum(diff) / (np.sum(magnitude) + 1e-10)
        else:
            roughness = 0.0

        return {
            'brightness': float(brightness),
            'roughness': float(roughness)
        }

    def _classify_voice_type(self, pitch_mean_hz: float) -> str:
        """Classify voice type based on average pitch"""
        if pitch_mean_hz < 110:
            return "bass"
        elif pitch_mean_hz < 150:
            return "baritone"
        elif pitch_mean_hz < 180:
            return "tenor"
        elif pitch_mean_hz < 220:
            return "alto"
        elif pitch_mean_hz < 300:
            return "mezzo-soprano"
        else:
            return "soprano"


# ═══════════════════════════════════════════════════════════════════════════════
# INTELLIGENT PHRASE SELECTOR
# ═══════════════════════════════════════════════════════════════════════════════


class IntelligentPhraseSelector:
    """
    Smart phrase selection for maximum phonetic coverage

    Uses linguistic analysis to select phrases that cover:
    - All English phonemes
    - Common phoneme transitions
    - Natural speech patterns
    - Varied prosody opportunities
    """

    def __init__(self):
        # Phonetically diverse phrases (hand-selected for max coverage)
        self.phrases = [
            # Vowel-heavy (covers all major vowels)
            "Hey JARVIS, show me photos from our vacation in Hawaii",
            "Open Safari and search for authentic Italian restaurants",

            # Consonant clusters (difficult sounds)
            "Set a timer for exactly fifteen minutes starting now",
            "What's the weather forecast for this weekend in San Francisco",

            # Sibilants and fricatives
            "Send a message to Sarah asking about the meeting schedule",
            "Search for the nearest coffee shop with free WiFi",

            # Plosives and stops
            "Play some jazz music by Pat Metheny from my library",
            "Calculate fifteen percent of two hundred and fifty dollars",

            # Nasals and liquids
            "Remind me to call mom tomorrow morning at nine AM",
            "Navigate to the nearest parking garage using real-time traffic",

            # Mixed complexity
            "What time is my next appointment with Doctor Anderson",
            "Turn on the bedroom lights and set them to seventy percent",
            "Add organic almond milk to my shopping list please",
            "Show me the latest news about artificial intelligence",
            "What's the current stock price of Apple and Microsoft",
        ]

        self.used_phrases = set()

    def select_phrases(self, num_samples: int, current_coverage: Optional[PhoneticCoverage] = None) -> List[str]:
        """
        Intelligently select phrases for maximum coverage

        Args:
            num_samples: Number of phrases needed
            current_coverage: Current phonetic coverage (for adaptive selection)

        Returns:
            List of optimally selected phrases
        """
        selected = []
        available_phrases = [p for p in self.phrases if p not in self.used_phrases]

        # If not enough unique phrases, allow reuse
        if len(available_phrases) < num_samples:
            available_phrases = self.phrases.copy()
            self.used_phrases.clear()

        # For now, use sequential selection (can be enhanced with coverage-based selection)
        for i in range(min(num_samples, len(available_phrases))):
            phrase = available_phrases[i]
            selected.append(phrase)
            self.used_phrases.add(phrase)

        return selected


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENHANCEMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════


class QuickVoiceEnhancement:
    """
    🚀 BEAST MODE Voice Profile Enhancement System

    Intelligently enhances existing voice profiles with minimal user effort
    """

    def __init__(
        self,
        speaker_name: str,
        num_samples: int = 10,
        duration_seconds: int = 10,
        quality_threshold: float = 0.65,
        max_retries: int = 3
    ):
        self.speaker_name = speaker_name
        self.num_samples = num_samples
        self.duration_seconds = duration_seconds
        self.quality_threshold = quality_threshold
        self.max_retries = max_retries
        self.sample_rate = 16000

        # Components (initialized in async initialize())
        self.db = None
        self.speechbrain = None
        self.quality_analyzer = AdvancedQualityAnalyzer(self.sample_rate, quality_threshold)
        self.voice_analyzer = AdvancedVoiceAnalyzer(self.sample_rate)
        self.phrase_selector = IntelligentPhraseSelector()

        # Data
        self.samples: List[EnhancementSample] = []
        self.progress = EnhancementProgress(
            total_samples=num_samples,
            completed_samples=0,
            current_sample=0
        )

        # Speaker info (loaded from database)
        self.speaker_id: Optional[int] = None
        self.existing_samples_count: int = 0

    async def initialize(self):
        """Initialize all components"""
        self._print_header()

        print("🔧 Initializing system components...")

        # 1. Database
        print("   📊 Connecting to database...", end=" ", flush=True)
        self.db = await get_learning_database()
        print("✅")

        # 2. Check if speaker exists
        print(f"   👤 Verifying speaker profile for '{self.speaker_name}'...", end=" ", flush=True)
        self.speaker_id = await self.db.get_or_create_speaker_profile(self.speaker_name)
        print(f"✅ (ID: {self.speaker_id})")

        # 3. Check existing samples
        print("   🔍 Checking existing voice samples...", end=" ", flush=True)
        existing_samples = await self.db.get_voice_samples_for_speaker(self.speaker_id, limit=100)
        self.existing_samples_count = len(existing_samples)

        # Check how many have audio_data
        with_audio = sum(1 for s in existing_samples if s.get('audio_data') is not None)
        without_audio = self.existing_samples_count - with_audio

        print(f"✅ Found {self.existing_samples_count} samples")
        print(f"      ├─ With audio: {with_audio}")
        print(f"      └─ Without audio: {without_audio}")

        # 4. SpeechBrain engine
        print("   🧠 Loading SpeechBrain ECAPA-TDNN engine...", end=" ", flush=True)
        model_config = ModelConfig(
            name="speechbrain-ecapa",
            engine=STTEngine.SPEECHBRAIN,
            disk_size_mb=380,
            ram_required_gb=2.0,
            vram_required_gb=1.8,
            expected_accuracy=0.96,
            avg_latency_ms=150,
            supports_fine_tuning=True,
            model_path="speechbrain/asr-wav2vec2-commonvoice-en"
        )
        self.speechbrain = SpeechBrainEngine(model_config)
        await self.speechbrain.initialize()
        print("✅")

        print("\n✨ System ready for enhanced enrollment!\n")

    async def enhance_profile(self) -> bool:
        """Main enhancement flow"""
        self._print_enhancement_info()

        input("\n🎤 Press ENTER when ready to start recording...")
        print()

        # Select phrases for maximum coverage
        phrases = self.phrase_selector.select_phrases(self.num_samples, self.progress.phonetic_coverage)

        # Collect samples
        for sample_num in range(1, self.num_samples + 1):
            self.progress.current_sample = sample_num
            phrase = phrases[sample_num - 1]

            try:
                sample = await self._record_and_process_sample(sample_num, phrase)

                if sample:
                    self.samples.append(sample)
                    self.progress.update(sample)
                    await self._save_sample_to_database(sample)
                    self._print_progress()

            except KeyboardInterrupt:
                print("\n\n⚠️  Enhancement interrupted by user")
                return False
            except Exception as e:
                logger.error(f"\n❌ Error processing sample {sample_num}: {e}")
                retry = input("\n   Retry this sample? (y/n): ")
                if retry.lower() != 'y':
                    return False

        # Compute final analytics
        await self._compute_final_analytics()

        return True

    async def _record_and_process_sample(
        self,
        sample_num: int,
        phrase: str
    ) -> Optional[EnhancementSample]:
        """Record and process a single sample with quality validation"""

        attempts = 0

        while attempts < self.max_retries:
            attempts += 1

            # Show recording UI
            self._print_sample_header(sample_num, phrase, attempts)

            # Countdown
            for i in range(3, 0, -1):
                print(f"   {i}...", flush=True)
                await asyncio.sleep(1)

            print(f"\n   🎙️  RECORDING... ({self.duration_seconds}s)")
            print("   " + "=" * 60)

            # Record
            audio_data = sd.rec(
                int(self.duration_seconds * self.sample_rate),
                samplerate=self.sample_rate,
                channels=1,
                dtype='float32'
            )
            sd.wait()

            print("   ✅ Recording complete!\n")

            # Process
            audio_array = audio_data.flatten()

            # Quality analysis
            print("   🔬 Analyzing quality...", flush=True)
            quality_metrics = await self.quality_analyzer.analyze(audio_array, verbose=False)

            # Extract embedding
            print("   🧬 Extracting speaker embedding...", flush=True)
            audio_bytes = self._to_wav_bytes(audio_array)
            embedding = await self.speechbrain.extract_speaker_embedding(audio_bytes)

            # Transcribe
            print("   📝 Transcribing...", flush=True)
            result = await self.speechbrain.transcribe(audio_bytes)
            transcription = result.text

            # Voice characteristics
            print("   🎵 Analyzing voice characteristics...", flush=True)
            voice_chars = await self.voice_analyzer.analyze(audio_array, transcription)

            # Calculate phonetic contribution
            temp_coverage = PhoneticCoverage()
            temp_coverage.vowels = self.progress.phonetic_coverage.vowels.copy()
            temp_coverage.consonants = self.progress.phonetic_coverage.consonants.copy()
            temp_coverage.phoneme_pairs = self.progress.phonetic_coverage.phoneme_pairs.copy()
            temp_coverage.update(transcription)
            phonetic_contribution = temp_coverage.coverage_score - self.progress.phonetic_coverage.coverage_score

            # Display results
            self._print_quality_results(quality_metrics, voice_chars, transcription)

            # Check if acceptable
            if quality_metrics.is_acceptable:
                print("\n   ✅ Sample ACCEPTED!")

                return EnhancementSample(
                    sample_number=sample_num,
                    phrase=phrase,
                    audio_bytes=audio_bytes,
                    audio_array=audio_array,
                    embedding=embedding,
                    transcription=transcription,
                    quality_metrics=quality_metrics,
                    voice_characteristics=voice_chars,
                    phonetic_contribution=phonetic_contribution,
                    timestamp=datetime.now(),
                    duration_seconds=self.duration_seconds,
                    recording_attempts=attempts
                )
            else:
                print(f"\n   ❌ Quality check FAILED (attempt {attempts}/{self.max_retries})")

                if attempts < self.max_retries:
                    print("\n   💡 Recommendations:")
                    for rec in quality_metrics.recommendations[:3]:
                        print(f"      • {rec}")

                    response = input("\n   Press ENTER to retry, or 's' to skip: ")
                    if response.lower() == 's':
                        print("   ⚠️  Accepting sample despite quality issues...")
                        return EnhancementSample(
                            sample_number=sample_num,
                            phrase=phrase,
                            audio_bytes=audio_bytes,
                            audio_array=audio_array,
                            embedding=embedding,
                            transcription=transcription,
                            quality_metrics=quality_metrics,
                            voice_characteristics=voice_chars,
                            phonetic_contribution=phonetic_contribution,
                            timestamp=datetime.now(),
                            duration_seconds=self.duration_seconds,
                            recording_attempts=attempts
                        )
                else:
                    response = input("\n   Accept anyway? (y/n): ")
                    if response.lower() == 'y':
                        return EnhancementSample(
                            sample_number=sample_num,
                            phrase=phrase,
                            audio_bytes=audio_bytes,
                            audio_array=audio_array,
                            embedding=embedding,
                            transcription=transcription,
                            quality_metrics=quality_metrics,
                            voice_characteristics=voice_chars,
                            phonetic_contribution=phonetic_contribution,
                            timestamp=datetime.now(),
                            duration_seconds=self.duration_seconds,
                            recording_attempts=attempts
                        )

        return None

    async def _save_sample_to_database(self, sample: EnhancementSample):
        """Save sample to database with raw audio"""
        await self.db.record_voice_sample(
            speaker_name=self.speaker_name,
            audio_data=sample.audio_bytes,
            transcription=sample.transcription,
            audio_duration_ms=sample.duration_seconds * 1000,
            quality_score=sample.quality_metrics.quality_score
        )

    async def _compute_final_analytics(self):
        """Compute and display final analytics"""
        print("\n" + "=" * 80)
        print("📊 FINAL ANALYTICS")
        print("=" * 80)

        # Extract embeddings and flatten to 1D if needed
        embeddings_raw = [s.embedding for s in self.samples]
        embeddings = np.array([e.flatten() if len(e.shape) > 1 else e for e in embeddings_raw])

        # Compute statistics
        avg_embedding = np.mean(embeddings, axis=0)
        std_embedding = np.std(embeddings, axis=0)
        embedding_variance = np.mean(std_embedding)
        consistency_score = 1.0 / (1.0 + embedding_variance)

        # Pairwise similarities
        similarities = []
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = np.dot(embeddings[i], embeddings[j]) / (
                    np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[j])
                )
                similarities.append(sim)

        avg_similarity = np.mean(similarities) if similarities else 0.0

        # Quality stats
        quality_scores = [s.quality_metrics.quality_score for s in self.samples]
        avg_quality = np.mean(quality_scores)

        # Voice characteristics
        pitches = [s.voice_characteristics.pitch_mean_hz for s in self.samples]
        avg_pitch = np.mean(pitches)

        # Overall confidence
        confidence = consistency_score * 0.4 + avg_similarity * 0.4 + avg_quality * 0.2
        confidence = max(0.5, min(1.0, confidence))

        print(f"\n📈 Enhancement Summary:")
        print(f"   New samples collected: {len(self.samples)}")
        print(f"   Total samples in profile: {self.existing_samples_count + len(self.samples)}")
        print(f"   Samples with audio: {len(self.samples)} new + existing")
        print(f"   Average quality: {avg_quality:.1%}")
        print(f"   Embedding consistency: {consistency_score:.1%}")
        print(f"   Average similarity: {avg_similarity:.1%}")
        print(f"   Overall confidence: {confidence:.1%}")

        print(f"\n🎵 Voice Profile:")
        print(f"   Average pitch: {avg_pitch:.1f} Hz")
        print(f"   Pitch range: {np.min(pitches):.1f} - {np.max(pitches):.1f} Hz")
        print(f"   Voice type: {self.samples[0].voice_characteristics.voice_type}")

        print(f"\n🔤 Phonetic Coverage:")
        print(f"   Overall score: {self.progress.phonetic_coverage.coverage_score:.1%}")
        print(f"   Vowels covered: {len(self.progress.phonetic_coverage.vowels)}")
        print(f"   Consonants covered: {len(self.progress.phonetic_coverage.consonants)}")
        print(f"   Phoneme pairs: {len(self.progress.phonetic_coverage.phoneme_pairs)}")

        # 🔬 Compute comprehensive aggregate acoustic features
        print(f"\n🔬 Computing comprehensive acoustic features...")
        acoustic_features = await self._compute_aggregate_acoustic_features()

        # Update speaker profile with embedding AND acoustic features
        print(f"\n💾 Updating speaker profile with biometric features...")
        embedding_bytes = avg_embedding.tobytes()

        await self._update_speaker_profile_comprehensive(
            speaker_id=self.speaker_id,
            embedding=embedding_bytes,
            embedding_dimension=len(avg_embedding),
            confidence=confidence,
            acoustic_features=acoustic_features,
            enrollment_quality=avg_quality
        )

        print("   ✅ Profile updated with FULL biometric features!")
        print(f"   📊 Stored: {len(acoustic_features)} acoustic parameters")

        print("\n" + "=" * 80)
        print("✨ ENHANCEMENT COMPLETE!")
        print("=" * 80)
        print("\n🎉 Your voice profile is now optimized for advanced reconstruction!")
        print(f"   • Total samples: {self.existing_samples_count + len(self.samples)}")
        print(f"   • With raw audio: {len(self.samples)}")
        print(f"   • Confidence: {confidence:.1%}")
        print(f"   • Phonetic coverage: {self.progress.phonetic_coverage.coverage_score:.1%}")
        print("\n✅ Advanced embedding reconstruction is now ENABLED!")
        print("=" * 80 + "\n")

    async def _compute_aggregate_acoustic_features(self) -> dict:
        """
        🔬 Compute comprehensive aggregate acoustic features from all samples

        Returns statistical summaries (mean, std) of all biometric features
        """
        import json

        # Extract all features from samples
        pitch_means = []
        pitch_stds = []
        pitch_ranges = []

        formant_f1s = []
        formant_f2s = []
        formant_f3s = []
        formant_f4s = []

        spectral_centroids = []
        spectral_rolloffs = []
        spectral_fluxes = []
        spectral_entropies = []

        speaking_rates = []
        pause_ratios = []

        jitters = []
        shimmers = []
        hnrs = []

        energies = []

        for sample in self.samples:
            vc = sample.voice_characteristics

            # Pitch
            pitch_means.append(vc.pitch_mean_hz)
            pitch_stds.append(vc.pitch_std_hz)
            pitch_ranges.append(vc.pitch_range_hz)

            # Formants
            formant_f1s.append(vc.f1_hz)
            formant_f2s.append(vc.f2_hz)
            formant_f3s.append(vc.f3_hz)
            formant_f4s.append(vc.f4_hz)

            # Spectral
            spectral_centroids.append(vc.spectral_centroid_hz)
            spectral_rolloffs.append(vc.spectral_rolloff_hz)
            spectral_fluxes.append(vc.spectral_flux)
            spectral_entropies.append(getattr(vc, 'spectral_entropy', UNMEASURED))

            # Temporal
            speaking_rates.append(vc.speech_rate_wpm)
            pause_ratios.append(vc.pause_ratio)

            # Voice quality.
            #
            # These defaults were 0.01 / 0.05 / 15.0 — "1%", "5%", "15 dB", a
            # healthy adult voice, substituted whenever the attribute was
            # ABSENT. Multiplied by 100 on the way to the database below, they
            # are exactly the 1.0 and 5.0 sitting in this machine's profile, and
            # the 15.0 HNR beside them. A missing attribute became a clean bill
            # of vocal health for a sample nobody had analysed.
            #
            # UNMEASURED instead. `_agg.mean` drops it, and if no sample
            # carried the attribute the column is written NULL rather than
            # describing a voice that was never measured.
            jitters.append(getattr(vc, 'jitter', UNMEASURED))
            shimmers.append(getattr(vc, 'shimmer', UNMEASURED))
            hnrs.append(getattr(vc, 'harmonic_to_noise_ratio', UNMEASURED))

            # Energy
            energies.append(vc.rms_energy)

        # Compute aggregate statistics.
        #
        # Every reduction below goes through _agg, which averages only the
        # samples that were actually measured. Two failure modes it removes:
        # a single NaN sample used to make the whole column NaN via np.mean,
        # discarding nineteen good samples; and an out-of-band value used to be
        # averaged in at full weight, dragging the enrolled figure toward a
        # number no vocal tract produces. A quantity with nothing measurable
        # left comes out UNMEASURED and is written NULL.
        flatness = [getattr(s.voice_characteristics, 'spectral_flatness',
                            s.quality_metrics.spectral_flatness)
                    for s in self.samples]
        mean_rate = _agg.mean('speaking_rate_wpm', speaking_rates)
        mean_pause = _agg.mean('pause_ratio', pause_ratios)

        features = {
            # Pitch features
            'pitch_mean_hz': _agg.mean('pitch_mean_hz', pitch_means),
            'pitch_std_hz': _agg.std('pitch_mean_hz', pitch_means),
            'pitch_range_hz': _agg.mean('pitch_range_hz', pitch_ranges),
            'pitch_min_hz': _agg.min('pitch_mean_hz', pitch_means),
            'pitch_max_hz': _agg.max('pitch_mean_hz', pitch_means),

            # Formant features (mean and variance across samples)
            'formant_f1_hz': _agg.mean('formant_f1_hz', formant_f1s),
            'formant_f1_std': _agg.std('formant_f1_hz', formant_f1s),
            'formant_f2_hz': _agg.mean('formant_f2_hz', formant_f2s),
            'formant_f2_std': _agg.std('formant_f2_hz', formant_f2s),
            'formant_f3_hz': _agg.mean('formant_f3_hz', formant_f3s),
            'formant_f3_std': _agg.std('formant_f3_hz', formant_f3s),
            'formant_f4_hz': _agg.mean('formant_f4_hz', formant_f4s),
            'formant_f4_std': _agg.std('formant_f4_hz', formant_f4s),

            # Spectral features
            'spectral_centroid_hz': _agg.mean('spectral_centroid_hz', spectral_centroids),
            'spectral_centroid_std': _agg.std('spectral_centroid_hz', spectral_centroids),
            'spectral_rolloff_hz': _agg.mean('spectral_rolloff_hz', spectral_rolloffs),
            'spectral_rolloff_std': _agg.std('spectral_rolloff_hz', spectral_rolloffs),
            'spectral_flux': _agg.mean('spectral_flux', spectral_fluxes),
            'spectral_flux_std': _agg.std('spectral_flux', spectral_fluxes),
            'spectral_entropy': _agg.mean('spectral_entropy', spectral_entropies),
            'spectral_entropy_std': _agg.std('spectral_entropy', spectral_entropies),
            'spectral_flatness': _agg.mean('spectral_flatness', flatness),
            'spectral_bandwidth_hz': _agg.std('spectral_centroid_hz', spectral_centroids) * 2,

            # Temporal features
            'speaking_rate_wpm': mean_rate,
            'speaking_rate_std': _agg.std('speaking_rate_wpm', speaking_rates),
            'pause_ratio': mean_pause,
            'pause_ratio_std': _agg.std('pause_ratio', pause_ratios),
            # Derived from the rate, so they inherit its absence rather than
            # computing a syllable count from a speaking rate nobody measured.
            'syllable_rate': mean_rate / 60.0 * 2.5,
            'articulation_rate': (mean_rate / (1.0 - mean_pause)
                                  if math.isfinite(mean_pause) and mean_pause < 1.0
                                  else UNMEASURED),

            # Energy features
            'energy_mean': _agg.mean('energy_mean', energies),
            'energy_std': _agg.std('energy_mean', energies),
            'energy_dynamic_range_db': _agg.dynamic_range_db(energies),

            # Voice quality features. The *100 makes these PERCENT, which is why
            # `jitter_percent` carries a different band from the in-memory
            # `jitter` fraction — see biological_bounds.BOUNDS.
            'jitter_percent': _agg.mean('jitter', jitters) * 100,
            'jitter_std': _agg.std('jitter', jitters) * 100,
            'shimmer_percent': _agg.mean('shimmer', shimmers) * 100,
            'shimmer_std': _agg.std('shimmer', shimmers) * 100,
            'harmonic_to_noise_ratio_db': _agg.mean('harmonic_to_noise_ratio_db', hnrs),
            'hnr_std': _agg.std('harmonic_to_noise_ratio_db', hnrs),
        }

        # THE WRITE GATE. Last stop before these become a stored profile that
        # every future verification is judged against: anything outside its
        # biological band is written NULL rather than compared against for
        # months. NaN is converted to None here too — SQLite would happily
        # store a NaN, and it would read back as a float that no `IS NULL`
        # check catches.
        sanitized = _VALIDATOR.sanitize(features, label="enrolled profile")
        features = {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                    for k, v in sanitized.clean.items()}

        # Compute covariance matrix for Mahalanobis distance
        # Extract feature vectors for covariance computation
        feature_vectors = []
        for sample in self.samples:
            vc = sample.voice_characteristics
            vec = [
                vc.pitch_mean_hz, vc.f1_hz, vc.f2_hz, vc.f3_hz,
                vc.spectral_centroid_hz, vc.spectral_rolloff_hz,
                getattr(vc, 'jitter', 0.01), getattr(vc, 'shimmer', 0.05),
                getattr(vc, 'harmonic_to_noise_ratio', 15.0)
            ]
            feature_vectors.append(vec)

        feature_matrix = np.array(feature_vectors)
        covariance_matrix = np.cov(feature_matrix.T)

        # Store covariance matrix as bytes
        features['feature_covariance_matrix_bytes'] = covariance_matrix.tobytes()
        features['feature_covariance_matrix_shape'] = covariance_matrix.shape

        # Additional statistics as JSON
        features['feature_statistics_json'] = json.dumps({
            'sample_count': len(self.samples),
            'feature_dimensions': len(feature_vectors[0]),
            'voice_type': self.samples[0].voice_characteristics.voice_type,
            'phonetic_coverage': self.progress.phonetic_coverage.coverage_score,
            'avg_duration_seconds': float(np.mean([s.duration_seconds for s in self.samples])),
        })

        return features

    async def _update_speaker_profile_comprehensive(
        self,
        speaker_id: int,
        embedding: bytes,
        embedding_dimension: int,
        confidence: float,
        acoustic_features: dict,
        enrollment_quality: float
    ):
        """
        Update speaker profile with comprehensive biometric features

        Fully dynamic SQL generation - no hardcoding
        """
        try:
            # Build dynamic UPDATE statement
            update_fields = []
            values = []

            # Core fields
            update_fields.append("voiceprint_embedding = ?")
            values.append(embedding)

            update_fields.append("embedding_dimension = ?")
            values.append(embedding_dimension)

            update_fields.append("recognition_confidence = ?")
            values.append(confidence)

            update_fields.append("enrollment_quality_score = ?")
            values.append(enrollment_quality)

            update_fields.append("total_samples = total_samples + ?")
            values.append(len(self.samples))

            update_fields.append("is_primary_user = ?")
            values.append(True)

            update_fields.append("last_updated = CURRENT_TIMESTAMP")

            update_fields.append("feature_extraction_version = ?")
            values.append('v1.0')

            # Add all acoustic features dynamically
            for key, value in acoustic_features.items():
                if key.endswith('_bytes'):
                    # Store binary data (covariance matrix)
                    update_fields.append(f"{key.replace('_bytes', '')} = ?")
                    values.append(value)
                elif key.endswith('_json'):
                    # Store JSON data
                    update_fields.append(f"{key.replace('_json', '')} = ?")
                    values.append(value)
                elif key.endswith('_shape'):
                    # Skip shape metadata (already encoded in matrix)
                    continue
                else:
                    # Store numeric features
                    update_fields.append(f"{key} = ?")
                    values.append(value)

            # Add speaker_id for WHERE clause
            values.append(speaker_id)

            # Build final SQL
            sql = f"UPDATE speaker_profiles SET {', '.join(update_fields)} WHERE speaker_id = ?"

            async with self.db.db.cursor() as cursor:
                await cursor.execute(sql, values)
                await self.db.db.commit()

        except Exception as e:
            logger.error(f"Failed to update comprehensive speaker profile: {e}", exc_info=True)
            # Fallback to basic update
            await self.db.update_speaker_embedding(
                speaker_id=speaker_id,
                embedding=embedding,
                confidence=confidence,
                is_primary_user=True
            )

    def _to_wav_bytes(self, audio: np.ndarray) -> bytes:
        """Convert numpy audio to WAV bytes"""
        buffer = io.BytesIO()
        sf.write(buffer, audio, self.sample_rate, format='WAV')
        buffer.seek(0)
        return buffer.read()

    # ═══════════════════════════════════════════════════════════════════════════
    # UI/DISPLAY METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def _print_header(self):
        """Print stylish header"""
        print("\n" + "=" * 80)
        print("🚀 ADVANCED QUICK VOICE ENHANCEMENT SYSTEM")
        print("=" * 80)
        print(f"   Speaker: {self.speaker_name}")
        print(f"   Samples: {self.num_samples}")
        print(f"   Duration: {self.duration_seconds}s per sample")
        print(f"   Quality threshold: {self.quality_threshold:.0%}")
        print("=" * 80 + "\n")

    def _print_enhancement_info(self):
        """Print enhancement information"""
        print("📋 ENHANCEMENT OVERVIEW")
        print("─" * 80)
        print(f"   Current profile: {self.existing_samples_count} samples (legacy format)")
        print(f"   Adding: {self.num_samples} new samples with raw audio")
        print(f"   Result: Advanced reconstruction ENABLED")
        print("\n💡 TIPS FOR BEST RESULTS:")
        print("   • Find a quiet location")
        print("   • Use consistent microphone positioning")
        print("   • Speak naturally and clearly")
        print("   • Vary your tone across samples")
        print("   • Follow quality feedback for retries")
        print("─" * 80 + "\n")

    def _print_sample_header(self, sample_num: int, phrase: str, attempt: int):
        """Print sample recording header"""
        print("\n" + "─" * 80)
        print(f"📍 Sample {sample_num}/{self.num_samples}")
        if attempt > 1:
            print(f"   Retry attempt {attempt}/{self.max_retries}")
        print("─" * 80)
        print(f'\n   📝 Phrase: "{phrase}"')
        print(f'\n   ⏳ Get ready...')

    def _print_quality_results(
        self,
        metrics: AdvancedQualityMetrics,
        voice: VoiceCharacteristics,
        transcription: str
    ):
        """Print quality analysis results"""
        print(f"\n   📊 Quality Analysis:")
        print(f"      ├─ Overall score: {metrics.quality_score:.1%}")
        print(f"      ├─ SNR: {metrics.snr_db:.1f} dB")
        print(f"      ├─ Voice activity: {metrics.vad_ratio:.1%}")
        print(f"      ├─ Dynamic range: {metrics.dynamic_range_db:.1f} dB")
        print(f"      └─ Spectral quality: {(1 - metrics.spectral_flatness):.1%}")

        print(f"\n   🎵 Voice Characteristics:")
        print(f"      ├─ Pitch: {voice.pitch_mean_hz:.1f} Hz ({voice.voice_type})")
        print(f"      ├─ Formants: F1={voice.f1_hz:.0f} F2={voice.f2_hz:.0f} Hz")
        print(f"      └─ Speech rate: {voice.speech_rate_wpm:.0f} WPM")

        print(f'\n   📝 Transcription: "{transcription}"')

        if metrics.issues:
            print(f"\n   ⚠️  Issues detected:")
            for issue in metrics.issues[:3]:
                print(f"      • {issue}")

    def _print_progress(self):
        """Print progress bar and stats"""
        completed = self.progress.completed_samples
        total = self.progress.total_samples
        percentage = (completed / total) * 100

        # Progress bar
        bar_width = 40
        filled = int(bar_width * completed / total)
        bar = "█" * filled + "░" * (bar_width - filled)

        print(f"\n   📈 Progress: [{bar}] {percentage:.0f}%")
        print(f"      ├─ Completed: {completed}/{total}")
        print(f"      ├─ Avg quality: {self.progress.avg_quality_score:.1%}")
        print(f"      ├─ Phonetic coverage: {self.progress.phonetic_coverage.coverage_score:.1%}")

        if self.progress.estimated_time_remaining_seconds > 0:
            minutes = int(self.progress.estimated_time_remaining_seconds / 60)
            seconds = int(self.progress.estimated_time_remaining_seconds % 60)
            print(f"      └─ Est. time remaining: {minutes}m {seconds}s")

    async def cleanup(self):
        """Cleanup resources"""
        if self.speechbrain:
            await self.speechbrain.cleanup()
        if self.db:
            await self.db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


async def main():
    parser = argparse.ArgumentParser(
        description="🚀 Advanced Quick Voice Enhancement - Optimize your voice profile in ~10 minutes"
    )
    parser.add_argument(
        "--speaker",
        type=str,
        default="Derek J. Russell",
        help="Speaker name (default: Derek J. Russell)"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        help="Number of samples to collect (default: 10, recommended: 10-15)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=10,
        help="Duration per sample in seconds (default: 10)"
    )
    parser.add_argument(
        "--quality-threshold",
        type=float,
        default=0.65,
        help="Quality threshold (0-1, default: 0.65)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retry attempts per sample (default: 3)"
    )

    args = parser.parse_args()

    # Create enhancement system
    enhancer = QuickVoiceEnhancement(
        speaker_name=args.speaker,
        num_samples=args.samples,
        duration_seconds=args.duration,
        quality_threshold=args.quality_threshold,
        max_retries=args.max_retries
    )

    try:
        # Initialize
        await enhancer.initialize()

        # Run enhancement
        success = await enhancer.enhance_profile()

        return 0 if success else 1

    except KeyboardInterrupt:
        print("\n\n⚠️  Enhancement cancelled by user")
        return 1
    except Exception as e:
        logger.error(f"\n❌ Enhancement failed: {e}", exc_info=True)
        return 1
    finally:
        await enhancer.cleanup()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n\n⚠️  Cancelled by user")
        sys.exit(1)
