#!/usr/bin/env python3
"""
Advanced Voice Enrollment System for JARVIS
Production-grade enrollment with quality validation, resume support, and comprehensive analysis

Features:
- Audio quality validation (SNR, clipping, background noise)
- Real-time quality feedback and retry mechanisms
- Real ECAPA-TDNN embeddings from SpeechBrain
- Comprehensive speaker profile analysis
- Resume support for interrupted enrollment
- Voice characteristics analysis (pitch, formants)
- Interactive user feedback with progress tracking

Usage:
    python backend/voice/enroll_voice.py --speaker "Derek J. Russell" --samples 25
    python backend/voice/enroll_voice.py --speaker "Derek J. Russell" --resume
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from intelligence.learning_database import JARVISLearningDatabase
from voice.biological_bounds import UNMEASURED, MeasuredAggregator, default_validator
from voice.engines.speechbrain_engine import SpeechBrainEngine
from voice.formant_estimation import estimate_formants
from voice.stt_config import ModelConfig, STTEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path.home() / ".cache" / "jarvis" / "enrollment.log",
            mode="a",
        ),
    ],
)
logger = logging.getLogger(__name__)

# Enrolment writes the profile every later verification is judged against, so
# this is the highest-stakes place in the system for an unmeasured value to be
# stored as a measurement. It is also where 42.9 Hz / 80.0 Hz / 420 wpm came in.
_VALIDATOR = default_validator()

# Shared with quick_voice_enhancement, the other path that writes these same
# columns. Two enrolment paths averaging the same quantities by different rules
# is how the stored profile came to disagree with itself.
_AGG = MeasuredAggregator(_VALIDATOR)


def _fmt_hz(value: float) -> str:
    """Render a possibly-absent measurement for the operator, never as a number."""
    return f"{value:.1f} Hz" if np.isfinite(value) else "unmeasured"


@dataclass
class AudioQualityMetrics:
    """Comprehensive audio quality metrics"""

    snr_db: float
    clipping_ratio: float
    background_noise_level: float
    rms_level: float
    vad_ratio: float
    peak_amplitude: float
    dynamic_range_db: float
    silence_ratio: float
    is_acceptable: bool
    quality_score: float
    feedback: List[str]


@dataclass
class VoiceCharacteristics:
    """Voice acoustic characteristics"""

    pitch_mean_hz: float
    pitch_std_hz: float
    pitch_range_hz: float
    formant_f1_hz: float
    formant_f2_hz: float
    spectral_centroid_hz: float
    spectral_rolloff_hz: float
    zero_crossing_rate: float


@dataclass
class EnrollmentSample:
    """Individual enrollment sample data"""

    sample_number: int
    phrase: str
    audio_data: bytes
    embedding: np.ndarray
    transcription: str
    quality_metrics: AudioQualityMetrics
    voice_characteristics: VoiceCharacteristics
    timestamp: str
    duration_seconds: float


@dataclass
class EnrollmentProgress:
    """Enrollment progress state for resume support"""

    speaker_name: str
    num_samples_target: int
    samples_completed: List[int]
    samples: List[EnrollmentSample]
    started_at: str
    last_updated: str
    session_id: str


class AudioQualityAnalyzer:
    """Advanced audio quality analysis for enrollment validation"""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.min_snr_db = 10.0
        self.max_clipping_ratio = 0.02
        self.min_rms_level = 0.005
        self.max_rms_level = 0.95
        self.min_vad_ratio = 0.20
        self.min_quality_score = 0.50

    async def analyze_quality(
        self, audio_data: np.ndarray, verbose: bool = True
    ) -> AudioQualityMetrics:
        """
        Comprehensive audio quality analysis

        Args:
            audio_data: Audio samples as numpy array
            verbose: Print detailed feedback

        Returns:
            AudioQualityMetrics with detailed analysis
        """
        feedback = []

        # 1. Signal-to-Noise Ratio (SNR)
        snr_db = self._calculate_snr(audio_data)
        if snr_db < self.min_snr_db:
            feedback.append(f"Low SNR ({snr_db:.1f} dB). Move to quieter location.")
        elif verbose:
            feedback.append(f"Good SNR: {snr_db:.1f} dB")

        # 2. Clipping Detection
        clipping_ratio = self._detect_clipping(audio_data)
        if clipping_ratio > self.max_clipping_ratio:
            feedback.append(
                f"Audio clipping detected ({clipping_ratio:.1%}). Reduce microphone gain."
            )
        elif verbose:
            feedback.append(f"No clipping: {clipping_ratio:.1%}")

        # 3. Background Noise Level
        noise_level = self._estimate_noise_level(audio_data)
        if noise_level > 0.05:
            feedback.append(f"High background noise ({noise_level:.3f}). Find quieter location.")
        elif verbose:
            feedback.append(f"Low background noise: {noise_level:.3f}")

        # 4. RMS Level (volume)
        rms_level = float(np.sqrt(np.mean(audio_data**2)))
        if rms_level < self.min_rms_level:
            feedback.append(f"Too quiet ({rms_level:.3f}). Speak louder or move mic closer.")
        elif rms_level > self.max_rms_level:
            feedback.append(f"Too loud ({rms_level:.3f}). Speak softer or move mic away.")
        elif verbose:
            feedback.append(f"Good volume: {rms_level:.3f}")

        # 5. Voice Activity Detection (VAD)
        vad_ratio, silence_ratio = self._calculate_vad_ratio(audio_data)
        if vad_ratio < self.min_vad_ratio:
            feedback.append(f"Insufficient speech ({vad_ratio:.1%}). Speak more during recording.")
        elif verbose:
            feedback.append(f"Good speech activity: {vad_ratio:.1%}")

        # 6. Peak Amplitude
        peak_amplitude = float(np.max(np.abs(audio_data)))
        if peak_amplitude > 0.99:
            feedback.append("Peak amplitude too high. Risk of clipping.")

        # 7. Dynamic Range
        dynamic_range_db = self._calculate_dynamic_range(audio_data)
        if dynamic_range_db < 20:
            feedback.append(f"Low dynamic range ({dynamic_range_db:.1f} dB). Check microphone.")
        elif verbose:
            feedback.append(f"Dynamic range: {dynamic_range_db:.1f} dB")

        # Calculate overall quality score (0-1)
        quality_score = self._calculate_quality_score(
            snr_db,
            clipping_ratio,
            noise_level,
            rms_level,
            vad_ratio,
            dynamic_range_db,
        )

        # Determine if acceptable
        is_acceptable = (
            snr_db >= self.min_snr_db
            and clipping_ratio <= self.max_clipping_ratio
            and self.min_rms_level <= rms_level <= self.max_rms_level
            and vad_ratio >= self.min_vad_ratio
            and quality_score >= self.min_quality_score
        )

        if not is_acceptable:
            feedback.append(
                f"QUALITY CHECK FAILED (score: {quality_score:.1%}). Please retry this sample."
            )
        elif verbose:
            feedback.append(f"Quality check PASSED (score: {quality_score:.1%})")

        return AudioQualityMetrics(
            snr_db=snr_db,
            clipping_ratio=clipping_ratio,
            background_noise_level=noise_level,
            rms_level=rms_level,
            vad_ratio=vad_ratio,
            peak_amplitude=peak_amplitude,
            dynamic_range_db=dynamic_range_db,
            silence_ratio=silence_ratio,
            is_acceptable=is_acceptable,
            quality_score=quality_score,
            feedback=feedback,
        )

    def _calculate_snr(self, audio_data: np.ndarray) -> float:
        """Calculate Signal-to-Noise Ratio in dB"""
        # Estimate noise from quietest 20% of frames
        frame_size = self.sample_rate // 20  # 50ms frames
        num_frames = len(audio_data) // frame_size

        frame_energies = []
        for i in range(num_frames):
            frame = audio_data[i * frame_size : (i + 1) * frame_size]
            energy = np.mean(frame**2)
            frame_energies.append(energy)

        frame_energies = np.array(frame_energies)
        noise_energy = np.percentile(frame_energies, 20)
        signal_energy = np.mean(frame_energies)

        if noise_energy < 1e-10:
            return 60.0  # Very high SNR

        snr = 10 * np.log10(signal_energy / noise_energy)
        return float(snr)

    def _detect_clipping(self, audio_data: np.ndarray) -> float:
        """Detect audio clipping (samples at max amplitude)"""
        threshold = 0.99
        clipped_samples = np.sum(np.abs(audio_data) > threshold)
        clipping_ratio = clipped_samples / len(audio_data)
        return float(clipping_ratio)

    def _estimate_noise_level(self, audio_data: np.ndarray) -> float:
        """Estimate background noise level"""
        # Use first and last 0.5 seconds as noise estimate
        noise_duration = min(self.sample_rate // 2, len(audio_data) // 10)
        noise_start = audio_data[:noise_duration]
        noise_end = audio_data[-noise_duration:]
        noise_samples = np.concatenate([noise_start, noise_end])
        noise_level = np.sqrt(np.mean(noise_samples**2))
        return float(noise_level)

    def _calculate_vad_ratio(
        self, audio_data: np.ndarray, threshold: float = 0.0001
    ) -> Tuple[float, float]:
        """Calculate voice activity detection ratio"""
        frame_size = int(self.sample_rate * 0.03)  # 30ms frames
        num_frames = len(audio_data) // frame_size

        voice_frames = 0
        for i in range(num_frames):
            frame = audio_data[i * frame_size : (i + 1) * frame_size]
            frame_energy = np.mean(frame**2)
            if frame_energy > threshold:
                voice_frames += 1

        vad_ratio = voice_frames / num_frames if num_frames > 0 else 0.0
        silence_ratio = 1.0 - vad_ratio
        return float(vad_ratio), float(silence_ratio)

    def _calculate_dynamic_range(self, audio_data: np.ndarray) -> float:
        """Calculate dynamic range in dB"""
        peak = np.max(np.abs(audio_data))
        noise = self._estimate_noise_level(audio_data)

        if noise < 1e-10:
            return 96.0  # Theoretical max for 16-bit

        dynamic_range = 20 * np.log10(peak / noise)
        return float(dynamic_range)

    def _calculate_quality_score(
        self,
        snr_db: float,
        clipping_ratio: float,
        noise_level: float,
        rms_level: float,
        vad_ratio: float,
        dynamic_range_db: float,
    ) -> float:
        """Calculate overall quality score (0-1)"""
        # SNR score (0-1)
        snr_score = min(1.0, max(0.0, (snr_db - 5) / 35))  # 5-40 dB range

        # Clipping score (1 = no clipping)
        clipping_score = max(0.0, 1.0 - clipping_ratio / 0.05)

        # Noise score (1 = low noise)
        noise_score = max(0.0, 1.0 - noise_level / 0.1)

        # RMS score (1 = optimal)
        optimal_rms = 0.3
        rms_score = 1.0 - abs(rms_level - optimal_rms) / optimal_rms

        # VAD score
        vad_score = min(1.0, vad_ratio / 0.5)  # Optimal at 50%+

        # Dynamic range score
        dr_score = min(1.0, max(0.0, (dynamic_range_db - 20) / 60))

        # Weighted average
        weights = {
            "snr": 0.25,
            "clipping": 0.20,
            "noise": 0.20,
            "rms": 0.15,
            "vad": 0.15,
            "dynamic_range": 0.05,
        }

        quality_score = (
            weights["snr"] * snr_score
            + weights["clipping"] * clipping_score
            + weights["noise"] * noise_score
            + weights["rms"] * rms_score
            + weights["vad"] * vad_score
            + weights["dynamic_range"] * dr_score
        )

        return float(quality_score)


class VoiceCharacteristicsAnalyzer:
    """Analyze voice acoustic characteristics for speaker profiling"""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate

    async def analyze_characteristics(self, audio_data: np.ndarray) -> VoiceCharacteristics:
        """
        Extract comprehensive voice characteristics

        Args:
            audio_data: Audio samples as numpy array

        Returns:
            VoiceCharacteristics with acoustic features
        """
        # 1. Pitch analysis
        pitch_mean, pitch_std, pitch_range = self._analyze_pitch(audio_data)

        # 2. Formant analysis
        f1, f2 = self._analyze_formants(audio_data)

        # 3. Spectral features
        spectral_centroid = self._calculate_spectral_centroid(audio_data)
        spectral_rolloff = self._calculate_spectral_rolloff(audio_data)

        # 4. Zero crossing rate
        zcr = self._calculate_zero_crossing_rate(audio_data)

        return VoiceCharacteristics(
            pitch_mean_hz=pitch_mean,
            pitch_std_hz=pitch_std,
            pitch_range_hz=pitch_range,
            formant_f1_hz=f1,
            formant_f2_hz=f2,
            spectral_centroid_hz=spectral_centroid,
            spectral_rolloff_hz=spectral_rolloff,
            zero_crossing_rate=zcr,
        )

    def _analyze_pitch(self, audio_data: np.ndarray) -> Tuple[float, float, float]:
        """Analyze pitch using autocorrelation"""
        # Apply frame-wise pitch detection
        frame_size = 2048
        hop_size = 512
        pitches = []

        for i in range(0, len(audio_data) - frame_size, hop_size):
            frame = audio_data[i : i + frame_size]

            # Autocorrelation method
            correlation = np.correlate(frame, frame, mode="full")
            correlation = correlation[len(correlation) // 2 :]

            # Find peaks
            min_lag = int(self.sample_rate / 500)  # Max 500 Hz
            max_lag = int(self.sample_rate / 50)  # Min 50 Hz

            if max_lag < len(correlation):
                search_region = correlation[min_lag:max_lag]
                if len(search_region) > 0:
                    peak_lag = min_lag + np.argmax(search_region)
                    if correlation[peak_lag] > 0.3 * correlation[0]:
                        pitch = self.sample_rate / peak_lag
                        if 50 <= pitch <= 500:  # Reasonable voice range
                            pitches.append(pitch)

        if pitches:
            pitch_mean = _VALIDATOR.coerce("pitch_mean_hz", float(np.mean(pitches)))
            pitch_std = _VALIDATOR.coerce("pitch_std_hz", float(np.std(pitches)))
            pitch_range = _VALIDATOR.coerce(
                "pitch_range_hz", float(np.max(pitches) - np.min(pitches)))
        else:
            # No frame yielded a period. This used to enrol 150/20/50 Hz — an
            # invented average male voice — as THIS speaker's pitch, against
            # which he would then be compared for the life of the profile.
            logger.warning(
                "[Enroll] no voiced frame yielded a pitch period — enrolling "
                "pitch as unmeasured rather than as a default voice"
            )
            pitch_mean = pitch_std = pitch_range = UNMEASURED

        return pitch_mean, pitch_std, pitch_range

    def _analyze_formants(self, audio_data: np.ndarray) -> Tuple[float, float]:
        """
        Estimate F1 and F2 by linear prediction.

        THIS IS THE FUNCTION THAT WROTE 42.9 Hz AND 80.0 Hz. It claimed LPC in a
        comment and performed spectral peak-picking::

            peaks, _ = find_peaks(magnitude, height=np.max(magnitude)*0.1, distance=20)
            f1 = float(freqs[peaks[0]])   # "first two peaks approximate F1 and F2"
            f2 = float(freqs[peaks[1]])

        ``peaks`` is ordered by frequency, so ``peaks[0]`` and ``peaks[1]`` are
        the two LOWEST spectral peaks in the recording — DC offset, mains hum and
        desk rumble, never vowel resonances. With ``rfftfreq`` over a multi-second
        sample the bins are sub-Hz apart and ``distance=20`` excludes only ~4 Hz,
        so both "peaks" came out of the same low-frequency lobe. The values it
        stored for the operator, 42.9 Hz and 80.03 Hz, are an order of magnitude
        below the F1 of any human being.

        A formant is a pole of the vocal tract filter, not a peak of the signal
        spectrum — which is dominated by the glottal source. The shared estimator
        fits an all-pole model to voiced frames and reads the pole angles, and
        reports UNMEASURED rather than the old ``(500.0, 1500.0)`` textbook male
        average when it cannot resolve one.
        """
        f1, f2, _, _ = estimate_formants(audio_data, self.sample_rate, n_formants=4)
        return f1, f2

    def _calculate_spectral_centroid(self, audio_data: np.ndarray) -> float:
        """Calculate spectral centroid (brightness of sound)"""
        fft = np.fft.rfft(audio_data)
        magnitude = np.abs(fft)
        freqs = np.fft.rfftfreq(len(audio_data), 1 / self.sample_rate)

        centroid = np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10)
        return float(centroid)

    def _calculate_spectral_rolloff(self, audio_data: np.ndarray) -> float:
        """Calculate spectral rolloff (85% of spectral energy)"""
        fft = np.fft.rfft(audio_data)
        magnitude = np.abs(fft) ** 2
        cumulative_sum = np.cumsum(magnitude)
        total_energy = cumulative_sum[-1]

        rolloff_threshold = 0.85 * total_energy
        rolloff_idx = np.where(cumulative_sum >= rolloff_threshold)[0]

        if len(rolloff_idx) > 0:
            freqs = np.fft.rfftfreq(len(audio_data), 1 / self.sample_rate)
            rolloff = float(freqs[rolloff_idx[0]])
        else:
            rolloff = float(self.sample_rate / 2)

        return rolloff

    def _calculate_zero_crossing_rate(self, audio_data: np.ndarray) -> float:
        """Calculate zero crossing rate"""
        zero_crossings = np.sum(np.abs(np.diff(np.sign(audio_data)))) / 2
        zcr = zero_crossings / len(audio_data)
        return float(zcr)


class VoiceEnrollment:
    """
    Advanced voice enrollment system with quality validation and resume support

    Features:
    - Real-time quality checks with automatic retry
    - Real ECAPA-TDNN embeddings
    - Resume interrupted enrollment
    - Comprehensive voice profiling
    - Interactive progress tracking
    """

    def __init__(
        self,
        speaker_name: str,
        num_samples: int = 25,
        resume: bool = False,
        session_id: Optional[str] = None,
    ):
        self.speaker_name = speaker_name
        self.num_samples = num_samples
        self.sample_rate = 16000  # 16kHz for SpeechBrain
        self.duration_seconds = 10  # 10 seconds per sample
        self.resume = resume
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")

        # Components
        self.learning_db = None
        self.speechbrain_engine = None
        self.quality_analyzer = AudioQualityAnalyzer(self.sample_rate)
        self.voice_analyzer = VoiceCharacteristicsAnalyzer(self.sample_rate)

        # Enrollment data
        self.enrollment_samples: List[EnrollmentSample] = []
        self.progress: Optional[EnrollmentProgress] = None

        # Paths
        self.checkpoint_dir = Path.home() / ".cache" / "jarvis" / "enrollment_checkpoints"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = (
            self.checkpoint_dir / f"{self.speaker_name.replace(' ', '_')}_{self.session_id}.json"
        )

    async def initialize(self):
        """Initialize database, SpeechBrain engine, and load checkpoint if resuming"""
        logger.info("🚀 Initializing Advanced Voice Enrollment System...")
        logger.info(f"   Session ID: {self.session_id}")

        # Initialize learning database
        self.learning_db = JARVISLearningDatabase()
        await self.learning_db.initialize()
        logger.info("   ✓ Learning database ready")

        # Initialize SpeechBrain engine for real embeddings
        model_config = ModelConfig(
            name="speechbrain-wav2vec2",
            engine=STTEngine.SPEECHBRAIN,
            disk_size_mb=380,
            ram_required_gb=2.0,
            vram_required_gb=1.8,
            expected_accuracy=0.96,
            avg_latency_ms=150,
            supports_fine_tuning=True,
            model_path="speechbrain/asr-wav2vec2-commonvoice-en",
        )

        self.speechbrain_engine = SpeechBrainEngine(model_config)
        await self.speechbrain_engine.initialize()
        logger.info("   ✓ SpeechBrain engine with ECAPA-TDNN ready")

        # Load checkpoint if resuming
        if self.resume:
            await self._load_checkpoint()

        # Initialize progress tracking
        if self.progress is None:
            self.progress = EnrollmentProgress(
                speaker_name=self.speaker_name,
                num_samples_target=self.num_samples,
                samples_completed=[],
                samples=[],
                started_at=datetime.now().isoformat(),
                last_updated=datetime.now().isoformat(),
                session_id=self.session_id,
            )

        logger.info("✅ System initialized\n")

    async def record_sample(
        self, sample_num: int, phrase: str, max_retries: int = 3
    ) -> Tuple[bytes, np.ndarray, AudioQualityMetrics]:
        """
        Record a single voice sample with quality validation and retry

        Args:
            sample_num: Sample number (1-indexed)
            phrase: Phrase for user to say
            max_retries: Maximum retry attempts for quality failures

        Returns:
            Tuple of (audio_bytes, audio_array, quality_metrics)
        """
        retry_count = 0

        while retry_count < max_retries:
            print(f"\n{'='*70}")
            print(f"Sample {sample_num}/{self.num_samples}")
            if retry_count > 0:
                print(f"Retry attempt {retry_count}/{max_retries - 1}")
            print(f"{'='*70}")
            print(f'\n📝 Please say: "{phrase}"')
            print(f"\n💡 Tips:")
            print(f"   • Speak naturally and clearly")
            print(f"   • Maintain consistent volume")
            print(f"   • Minimize background noise")
            print(f"\n⏳ Get ready! Recording will start in...")

            # Countdown
            for i in range(3, 0, -1):
                print(f"   {i}...")
                await asyncio.sleep(1)

            print(f"\n🎙️  RECORDING NOW... (speak clearly for {self.duration_seconds} seconds)")
            print("=" * 70)

            # Record audio
            audio_data = sd.rec(
                int(self.duration_seconds * self.sample_rate),
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
            )
            sd.wait()  # Wait until recording is finished

            print("✅ Recording complete! Analyzing quality...")

            # Analyze audio quality
            audio_array = audio_data.flatten()
            quality_metrics = await self.quality_analyzer.analyze_quality(audio_array, verbose=True)

            # Display quality feedback
            print(f"\n📊 Quality Analysis:")
            print(f"   Overall Score: {quality_metrics.quality_score:.1%}")
            print(f"   SNR: {quality_metrics.snr_db:.1f} dB")
            print(f"   Voice Activity: {quality_metrics.vad_ratio:.1%}")
            print(f"   RMS Level: {quality_metrics.rms_level:.3f}")
            print(f"   Clipping: {quality_metrics.clipping_ratio:.2%}")

            # Show detailed feedback
            if quality_metrics.feedback:
                print(f"\n💬 Feedback:")
                for feedback in quality_metrics.feedback:
                    print(f"   • {feedback}")

            # Check if acceptable
            if quality_metrics.is_acceptable:
                print(f"\n✅ Quality check PASSED!")
                audio_bytes = self._audio_to_wav_bytes(audio_data)
                return audio_bytes, audio_array, quality_metrics
            else:
                retry_count += 1
                if retry_count < max_retries:
                    print(f"\n❌ Quality check FAILED. Please retry...")
                    response = input("\nPress ENTER to retry (or 's' to skip): ")
                    if response.lower() == "s":
                        print("⚠️  Accepting sample despite quality issues...")
                        audio_bytes = self._audio_to_wav_bytes(audio_data)
                        return audio_bytes, audio_array, quality_metrics
                else:
                    print(f"\n❌ Maximum retries reached.")
                    response = input("Accept anyway? (y/n): ")
                    if response.lower() == "y":
                        audio_bytes = self._audio_to_wav_bytes(audio_data)
                        return audio_bytes, audio_array, quality_metrics
                    else:
                        raise ValueError("Sample rejected due to quality issues")

    def _audio_to_wav_bytes(self, audio_data: np.ndarray) -> bytes:
        """Convert numpy audio to WAV bytes"""
        import io

        buffer = io.BytesIO()
        sf.write(buffer, audio_data, self.sample_rate, format="WAV")
        buffer.seek(0)
        return buffer.read()

    async def extract_speaker_embedding(self, audio_bytes: bytes) -> np.ndarray:
        """
        Extract real speaker embedding using ECAPA-TDNN from SpeechBrain

        Args:
            audio_bytes: Audio data as WAV bytes

        Returns:
            Speaker embedding as numpy array (192-dimensional from ECAPA-TDNN)
        """
        # Use real SpeechBrain ECAPA-TDNN embeddings
        embedding = await self.speechbrain_engine.extract_speaker_embedding(audio_bytes)
        return embedding

    async def enroll_speaker(self):
        """
        Advanced enrollment flow with quality validation, resume support, and comprehensive analysis
        """
        print("\n" + "=" * 70)
        print("🎤 JARVIS ADVANCED VOICE ENROLLMENT")
        print("=" * 70)
        print(f"\nEnrolling speaker: {self.speaker_name}")
        print(f"Number of samples: {self.num_samples}")
        print(f"Sample duration: {self.duration_seconds} seconds each")
        print(f"Session ID: {self.session_id}")

        if self.resume and len(self.enrollment_samples) > 0:
            print(f"\n🔄 Resuming from checkpoint...")
            print(f"   Already completed: {len(self.enrollment_samples)} samples")
            print(f"   Remaining: {self.num_samples - len(self.enrollment_samples)} samples")

        print("\n💡 Tips for best enrollment:")
        print("  • Find a quiet location")
        print("  • Speak naturally and clearly")
        print("  • Use your normal speaking volume")
        print("  • Vary your tone across samples")
        print("  • Position microphone consistently")
        print("  • Each sample will be validated for quality")
        print("\n" + "=" * 70)

        input("\nPress ENTER to start enrollment...")

        # Phrases for enrollment (varied to capture different phonemes)
        phrases = self._get_enrollment_phrases()

        # Determine which samples to collect
        completed_sample_nums = [s.sample_number for s in self.enrollment_samples]
        samples_to_collect = [
            i for i in range(1, self.num_samples + 1) if i not in completed_sample_nums
        ]

        # Collect samples
        for sample_num in samples_to_collect:
            phrase = phrases[(sample_num - 1) % len(phrases)]

            try:
                # Record with quality validation
                audio_bytes, audio_array, quality_metrics = await self.record_sample(
                    sample_num, phrase
                )

                # Extract real ECAPA-TDNN embedding
                print("   Extracting speaker embedding (ECAPA-TDNN)...")
                embedding = await self.extract_speaker_embedding(audio_bytes)
                print(f"   ✓ Embedding extracted ({embedding.shape[0]} dimensions)")

                # Transcribe
                print("   Transcribing audio...")
                result = await self.speechbrain_engine.transcribe(audio_bytes)
                transcription = result.text
                print(f'   ✓ Transcribed: "{transcription}"')

                # Analyze voice characteristics
                print("   Analyzing voice characteristics...")
                voice_chars = await self.voice_analyzer.analyze_characteristics(audio_array)
                print(f"   ✓ Pitch: {voice_chars.pitch_mean_hz:.1f} Hz")
                print(
                    f"   ✓ Formants: F1={voice_chars.formant_f1_hz:.0f} Hz, F2={voice_chars.formant_f2_hz:.0f} Hz"
                )

                # Create enrollment sample
                enrollment_sample = EnrollmentSample(
                    sample_number=sample_num,
                    phrase=phrase,
                    audio_data=audio_bytes,
                    embedding=embedding,
                    transcription=transcription,
                    quality_metrics=quality_metrics,
                    voice_characteristics=voice_chars,
                    timestamp=datetime.now().isoformat(),
                    duration_seconds=self.duration_seconds,
                )

                self.enrollment_samples.append(enrollment_sample)

                # Record in learning database
                await self.learning_db.record_voice_sample(
                    speaker_name=self.speaker_name,
                    audio_data=audio_bytes,
                    transcription=transcription,
                    audio_duration_ms=self.duration_seconds * 1000,
                    quality_score=quality_metrics.quality_score,
                )

                # Save checkpoint after each sample
                await self._save_checkpoint()

                print(f"\n✅ Sample {sample_num}/{self.num_samples} completed and saved!")
                print(
                    f"   Progress: {len(self.enrollment_samples)}/{self.num_samples} "
                    f"({len(self.enrollment_samples) / self.num_samples:.0%})"
                )

            except KeyboardInterrupt:
                logger.warning("\n⚠️  Enrollment interrupted by user")
                await self._save_checkpoint()
                print(f"\n💾 Progress saved. Resume with: --resume --session-id {self.session_id}")
                return False
            except Exception as e:
                logger.error(f"❌ Error recording sample {sample_num}: {e}")
                retry = input("Retry this sample? (y/n): ")
                if retry.lower() == "y":
                    # Don't increment, retry same sample
                    continue
                else:
                    await self._save_checkpoint()
                    return False

        # All samples collected - compute speaker profile
        await self._compute_speaker_profile()

        return True

    def _get_enrollment_phrases(self) -> List[str]:
        """Get phonetically diverse enrollment phrases"""
        return [
            "Hey JARVIS, what's the weather today?",
            "Open Safari and search for restaurants",
            "Connect to the living room TV",
            "Set a timer for 10 minutes",
            "What's on my calendar tomorrow?",
            "Turn on the lights in the bedroom",
            "Play some jazz music",
            "Send an email to my assistant",
            "What time is my next meeting?",
            "Navigate to the nearest coffee shop",
            "Add milk to my shopping list",
            "What's the latest news?",
            "Set an alarm for 7 AM",
            "Call my mom on speaker",
            "Show me photos from last week",
            "What's the stock price of Apple?",
            "Turn up the volume",
            "Pause the music",
            "What's the capital of France?",
            "Calculate 15 percent of 200",
            "Remind me to call John at 3 PM",
            "What's the traffic like to work?",
            "Turn off all the lights",
            "What's my heart rate?",
            "Read my latest messages",
        ]

    async def _compute_speaker_profile(self):
        """Compute comprehensive speaker profile from all samples"""
        print("\n" + "=" * 70)
        print("🧠 COMPUTING SPEAKER PROFILE")
        print("=" * 70)

        # Filter out placeholder samples (from checkpoint resume)
        real_samples = [s for s in self.enrollment_samples if len(s.audio_data) > 0]

        if len(real_samples) == 0:
            logger.error("No real samples found. Cannot compute profile.")
            return

        # Extract all embeddings
        embeddings = [s.embedding for s in real_samples]
        embeddings_array = np.array(embeddings)

        # Compute statistics
        avg_embedding = np.mean(embeddings_array, axis=0)
        std_embedding = np.std(embeddings_array, axis=0)

        # Embedding variance (consistency metric)
        embedding_variance = np.mean(std_embedding)
        consistency_score = 1.0 / (1.0 + embedding_variance)  # Higher = more consistent

        # Compute pairwise cosine similarities
        similarities = []
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = np.dot(embeddings[i], embeddings[j]) / (
                    np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[j])
                )
                similarities.append(sim)

        avg_similarity = np.mean(similarities) if similarities else 0.0

        # Confidence score (based on consistency and quality)
        quality_scores = [s.quality_metrics.quality_score for s in real_samples]
        avg_quality = np.mean(quality_scores)

        confidence = consistency_score * 0.4 + avg_similarity * 0.4 + avg_quality * 0.2
        confidence = max(0.5, min(1.0, confidence))

        # Aggregate voice characteristics
        pitches = [s.voice_characteristics.pitch_mean_hz for s in real_samples]
        formant_f1s = [s.voice_characteristics.formant_f1_hz for s in real_samples]
        formant_f2s = [s.voice_characteristics.formant_f2_hz for s in real_samples]

        # NaN-aware, and per-quantity. A single sample whose formants could not
        # be resolved must cost that one sample's contribution, not the whole
        # enrolment: plain ``np.mean`` over a list holding one NaN returns NaN
        # for every speaker, which would convert the honest per-sample absence
        # introduced above into a total enrolment failure. ``MeasuredAggregator``
        # drops the absent entries, and reports UNMEASURED only when nothing
        # measurable is left — the one case where there is genuinely nothing to
        # average.
        avg_pitch = _AGG.mean("pitch_mean_hz", pitches)
        avg_f1 = _AGG.mean("formant_f1_hz", formant_f1s)
        avg_f2 = _AGG.mean("formant_f2_hz", formant_f2s)

        # Display comprehensive statistics
        print(f"\n📊 Enrollment Statistics:")
        print(f"   Total samples: {len(real_samples)}")
        print(f"   Embedding dimensions: {len(avg_embedding)}")
        print(f"   Embedding variance: {embedding_variance:.4f}")
        print(f"   Consistency score: {consistency_score:.2%}")
        print(f"   Average similarity: {avg_similarity:.2%}")
        print(f"   Average quality: {avg_quality:.2%}")
        print(f"   Final confidence: {confidence:.2%}")

        # Printed via _fmt_hz so an unmeasured characteristic reads as
        # "unmeasured" rather than as "nan Hz" — the operator is the last line
        # of defence against a bad enrolment, and can only act on what the
        # summary actually tells them.
        measured_pitches = [p for p in pitches if np.isfinite(p)]
        print(f"\n🎵 Voice Characteristics:")
        print(f"   Average pitch: {_fmt_hz(avg_pitch)}")
        if measured_pitches:
            print(f"   Pitch range: {np.min(measured_pitches):.1f} - "
                  f"{np.max(measured_pitches):.1f} Hz")
        else:
            print(f"   Pitch range: unmeasured")
        print(f"   Formant F1: {_fmt_hz(avg_f1)}")
        print(f"   Formant F2: {_fmt_hz(avg_f2)}")

        print(f"\n📈 Quality Breakdown:")
        snr_values = [s.quality_metrics.snr_db for s in real_samples]
        vad_values = [s.quality_metrics.vad_ratio for s in real_samples]
        print(f"   Average SNR: {np.mean(snr_values):.1f} dB")
        print(f"   Average VAD ratio: {np.mean(vad_values):.1%}")
        print(f"   Quality range: {np.min(quality_scores):.1%} - {np.max(quality_scores):.1%}")

        # Calculate optimal enrollment time
        total_duration = sum(s.duration_seconds for s in real_samples)
        print(f"\n⏱️  Enrollment Time:")
        print(f"   Total audio: {total_duration:.0f} seconds ({total_duration / 60:.1f} minutes)")
        print(f"   Enrollment efficiency: {confidence:.1%}")

        # Store speaker profile in database
        print(f"\n💾 Storing speaker profile...")
        speaker_id = await self.learning_db.get_or_create_speaker_profile(self.speaker_name)

        # Serialize embedding
        embedding_bytes = avg_embedding.tobytes()

        await self.learning_db.update_speaker_embedding(
            speaker_id=speaker_id,
            embedding=embedding_bytes,
            confidence=confidence,
            is_primary_user=True,
        )

        # Delete checkpoint (enrollment complete)
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()
            print(f"   ✓ Checkpoint deleted (enrollment complete)")

        print("\n" + "=" * 70)
        print("✅ ENROLLMENT COMPLETE!")
        print("=" * 70)
        print(f"\nSpeaker Profile Created:")
        print(f"  Name: {self.speaker_name}")
        print(f"  Speaker ID: {speaker_id}")
        print(f"  Confidence: {confidence:.1%}")
        print(f"  Samples: {len(real_samples)}")
        print(f"  Embedding Dimensions: {len(avg_embedding)}")
        print(f"  Model: ECAPA-TDNN (SpeechBrain)")
        print(f"\n🎉 JARVIS can now recognize your voice!")
        print("=" * 70)

    async def _save_checkpoint(self):
        """Save enrollment progress to checkpoint file"""
        try:
            # Convert samples to serializable format
            checkpoint_data = {
                "speaker_name": self.speaker_name,
                "num_samples_target": self.num_samples,
                "session_id": self.session_id,
                "started_at": (
                    self.progress.started_at if self.progress else datetime.now().isoformat()
                ),
                "last_updated": datetime.now().isoformat(),
                "samples_completed": [s.sample_number for s in self.enrollment_samples],
                "num_completed": len(self.enrollment_samples),
            }

            # Save checkpoint
            with open(self.checkpoint_file, "w") as f:
                json.dump(checkpoint_data, f, indent=2)

            logger.debug(f"Checkpoint saved: {self.checkpoint_file}")

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")

    async def _load_checkpoint(self):
        """Load enrollment progress from checkpoint file"""
        try:
            # If specific session ID not provided, find latest checkpoint
            if self.session_id and not self.checkpoint_file.exists():
                # Try to find checkpoint with this session ID
                pattern = self.checkpoint_dir / f"*_{self.session_id}.json"
                matches = list(self.checkpoint_dir.glob(pattern.name))
                if matches:
                    self.checkpoint_file = matches[0]

            if not self.checkpoint_file.exists():
                logger.warning(f"No checkpoint found at {self.checkpoint_file}")
                return

            with open(self.checkpoint_file, "r") as f:
                checkpoint_data = json.load(f)

            logger.info(f"📂 Loading checkpoint from {self.checkpoint_file}")
            logger.info(f"   Samples completed: {checkpoint_data['num_completed']}")
            logger.info(f"   Started: {checkpoint_data['started_at']}")

            # Load progress metadata
            self.progress = EnrollmentProgress(
                speaker_name=checkpoint_data["speaker_name"],
                num_samples_target=checkpoint_data["num_samples_target"],
                samples_completed=checkpoint_data["samples_completed"],
                samples=[],  # Will load from database
                started_at=checkpoint_data["started_at"],
                last_updated=checkpoint_data["last_updated"],
                session_id=checkpoint_data["session_id"],
            )

            # Create placeholder EnrollmentSample objects for completed samples
            # We use placeholders to track which samples are done without storing full audio
            for sample_num in checkpoint_data["samples_completed"]:
                # Create minimal placeholder (will be skipped in enrollment loop)
                placeholder = EnrollmentSample(
                    sample_number=sample_num,
                    phrase="",
                    audio_data=b"",
                    embedding=np.zeros(192),  # ECAPA-TDNN size
                    transcription="",
                    quality_metrics=AudioQualityMetrics(
                        snr_db=0.0,
                        clipping_ratio=0.0,
                        background_noise_level=0.0,
                        rms_level=0.0,
                        vad_ratio=0.0,
                        peak_amplitude=0.0,
                        dynamic_range_db=0.0,
                        silence_ratio=0.0,
                        is_acceptable=True,
                        quality_score=0.0,
                        feedback=[],
                    ),
                    voice_characteristics=VoiceCharacteristics(
                        pitch_mean_hz=0.0,
                        pitch_std_hz=0.0,
                        pitch_range_hz=0.0,
                        formant_f1_hz=0.0,
                        formant_f2_hz=0.0,
                        spectral_centroid_hz=0.0,
                        spectral_rolloff_hz=0.0,
                        zero_crossing_rate=0.0,
                    ),
                    timestamp="",
                    duration_seconds=0.0,
                )
                self.enrollment_samples.append(placeholder)

            logger.info(f"✅ Checkpoint loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")
            self.progress = None

    async def cleanup(self):
        """Cleanup resources"""
        if self.speechbrain_engine:
            await self.speechbrain_engine.cleanup()
        if self.learning_db:
            await self.learning_db.close()


async def main():
    parser = argparse.ArgumentParser(
        description="Advanced Voice Enrollment for JARVIS with quality validation and resume support"
    )
    parser.add_argument(
        "--speaker",
        type=str,
        default="Derek J. Russell",
        help="Speaker name (default: Derek J. Russell)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=30,
        help="Number of voice samples to collect (default: 30)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh mode: Update existing profile with new samples",
    )
    parser.add_argument(
        "--auto-refresh",
        action="store_true",
        help="Enable automatic sample freshness management",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume interrupted enrollment session",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        help="Session ID for resume (defaults to latest)",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available checkpoint sessions",
    )

    args = parser.parse_args()

    # List sessions if requested
    if args.list_sessions:
        checkpoint_dir = Path.home() / ".cache" / "jarvis" / "enrollment_checkpoints"
        if checkpoint_dir.exists():
            checkpoints = list(checkpoint_dir.glob("*.json"))
            if checkpoints:
                print("\n📂 Available checkpoint sessions:")
                for cp in checkpoints:
                    with open(cp, "r") as f:
                        data = json.load(f)
                    print(f"\n  Session ID: {data['session_id']}")
                    print(f"  Speaker: {data['speaker_name']}")
                    print(f"  Progress: {data['num_completed']}/{data['num_samples_target']}")
                    print(f"  Last updated: {data['last_updated']}")
                    print(f"  File: {cp}")
            else:
                print("\n❌ No checkpoint sessions found")
        else:
            print("\n❌ No checkpoint directory found")
        return 0

    # Ensure cache directory exists
    cache_dir = Path.home() / ".cache" / "jarvis"
    cache_dir.mkdir(parents=True, exist_ok=True)

    enrollment = VoiceEnrollment(
        speaker_name=args.speaker,
        num_samples=args.samples,
        resume=args.resume,
        session_id=args.session_id,
    )

    try:
        await enrollment.initialize()
        success = await enrollment.enroll_speaker()

        if success:
            print("\n🎊 Voice enrollment successful!")
            return 0
        else:
            print("\n❌ Voice enrollment failed or was cancelled")
            return 1

    except Exception as e:
        logger.error(f"❌ Enrollment failed: {e}", exc_info=True)
        return 1

    finally:
        await enrollment.cleanup()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n\n⚠️  Enrollment cancelled by user")
        print("💡 Progress has been saved. Use --resume to continue.")
        sys.exit(1)
