#!/usr/bin/env python3
"""
🔬 ADVANCED BIOMETRIC FEATURE EXTRACTION
═══════════════════════════════════════════════════════════════════════════════

Extracts comprehensive voice biometric features:
- Deep learning embeddings (ECAPA-TDNN)
- Acoustic features (pitch, formants, spectral)
- Voice quality metrics (jitter, shimmer, HNR)
- Temporal characteristics (speaking rate, rhythm)
- Energy contours

All async with proper thread pool execution for CPU-intensive work.
Zero hardcoding, fully dynamic.

Author: Claude Code + Derek J. Russell
Version: 2.0.0 - Async Optimized
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import Optional, Tuple, Dict, Any

import numpy as np
import torch

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

# The bounds gate and the one formant estimator. Guarded the same way as the
# executor import above because this module is reached under both roots.
try:  # pragma: no cover - depends on which root is on sys.path
    from backend.voice.biological_bounds import BOUNDS, UNMEASURED, default_validator
    from backend.voice.formant_estimation import estimate_formants
except ImportError:  # pragma: no cover
    from voice.biological_bounds import BOUNDS, UNMEASURED, default_validator
    from voice.formant_estimation import estimate_formants

logger = logging.getLogger(__name__)

_VALIDATOR = default_validator()
_HNR_BAND = BOUNDS["harmonic_to_noise_ratio_db"]

# Shared thread pool for CPU-intensive feature extraction
# Using a dedicated pool prevents blocking the main event loop
_feature_executor: Optional[ThreadPoolExecutor] = None


def get_feature_executor() -> ThreadPoolExecutor:
    """Get or create the shared thread pool for feature extraction."""
    global _feature_executor
    if _feature_executor is None:
        # Use 4 workers for parallel feature extraction
        if _HAS_MANAGED_EXECUTOR:
            _feature_executor = ManagedThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="feature_extract",
                name="feature_extract"
            )
        else:
            _feature_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="feature_extract"
            )
        logger.info("🔧 Created feature extraction thread pool (4 workers)")
    return _feature_executor


class AdvancedFeatureExtractor:
    """
    🔬 Advanced biometric feature extraction with proper async execution.

    All CPU-intensive numpy/scipy operations run in a thread pool
    to prevent blocking the async event loop.
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._executor = get_feature_executor()

    async def _run_in_executor(self, func, *args, **kwargs) -> Any:
        """Run a CPU-intensive function in the thread pool."""
        loop = asyncio.get_running_loop()
        if kwargs:
            func = partial(func, **kwargs)
        return await loop.run_in_executor(self._executor, func, *args)

    async def extract_features(
        self,
        audio_tensor: torch.Tensor,
        embedding: np.ndarray,
        transcription: str = ""
    ) -> "VoiceBiometricFeatures":
        """
        Extract comprehensive biometric features.

        All CPU-intensive work runs in thread pool to avoid blocking.

        Args:
            audio_tensor: Audio as torch tensor
            embedding: Pre-computed ECAPA-TDNN embedding
            transcription: Optional transcription

        Returns:
            VoiceBiometricFeatures object
        """
        from voice.advanced_biometric_verification import VoiceBiometricFeatures

        # Convert to numpy (quick operation)
        # CRITICAL: Use .copy() to avoid memory corruption when tensor is GC'd
        audio_np = audio_tensor.cpu().numpy().copy() if torch.is_tensor(audio_tensor) else np.asarray(audio_tensor)

        # Extract all features in parallel using thread pool
        # Each extraction runs in its own thread, truly parallel
        results = await asyncio.gather(
            self._extract_pitch_features_async(audio_np),
            self._extract_formants_async(audio_np),
            self._extract_spectral_features_async(audio_np),
            self._extract_temporal_features_async(audio_np, transcription),
            self._extract_voice_quality_async(audio_np),
            return_exceptions=True
        )

        pitch_features, formants, spectral_features, temporal_features, quality_features = results

        # A stage that raised produced NO measurement. It used to produce a
        # textbook one — pitch 150/20/50, formants [500, 1500, 2500, 3500],
        # jitter 0.01, shimmer 0.05, HNR 15.0 — which is an average adult male
        # voice, emitted from a failure path, carrying the units and the type of
        # real data and nothing to mark it apart from real data. Compared
        # against an enrolled profile it is a confident statement about a voice
        # that was never analysed.
        #
        # Every substitute below is UNMEASURED instead, and the failure is
        # logged at WARNING with the exception type so a persistently broken
        # stage is visible rather than inferred from a suspiciously average
        # profile months later.
        if isinstance(pitch_features, BaseException):
            logger.warning("[Features] pitch stage failed (%s: %s) — pitch unmeasured",
                           type(pitch_features).__name__, pitch_features)
            pitch_features = {'mean': UNMEASURED, 'std': UNMEASURED, 'range': UNMEASURED}

        if isinstance(formants, BaseException):
            logger.warning("[Features] formant stage failed (%s: %s) — formants unmeasured",
                           type(formants).__name__, formants)
            formants = [UNMEASURED] * 4

        if isinstance(spectral_features, BaseException):
            logger.warning("[Features] spectral stage failed (%s: %s) — spectra unmeasured",
                           type(spectral_features).__name__, spectral_features)
            spectral_features = {'centroid': UNMEASURED, 'rolloff': UNMEASURED,
                                 'flux': UNMEASURED, 'entropy': UNMEASURED}

        if isinstance(temporal_features, BaseException):
            logger.warning("[Features] temporal stage failed (%s: %s) — rate unmeasured",
                           type(temporal_features).__name__, temporal_features)
            temporal_features = {'rate': UNMEASURED, 'pause_ratio': UNMEASURED,
                                 'energy': np.zeros(0)}

        if isinstance(quality_features, BaseException):
            logger.warning("[Features] quality stage failed (%s: %s) — jitter/shimmer/HNR unmeasured",
                           type(quality_features).__name__, quality_features)
            quality_features = {'jitter': UNMEASURED, 'shimmer': UNMEASURED, 'hnr': UNMEASURED}

        # Formant lists shorter than 4 are padded with absence, never with the
        # next textbook value, so positional indexing below stays safe.
        formants = list(formants) + [UNMEASURED] * max(0, 4 - len(formants))

        # THE GATE. Every DSP scalar crosses the biological bounds on its way
        # into the feature object, so a value that no human vocal tract can
        # produce becomes UNMEASURED here rather than being stored, compared
        # against, and eventually reported as a spoofing indicator.
        #
        # It is deliberately applied at the assembly point rather than inside
        # each extractor: this is the single seam every feature passes through,
        # so a stage added later is gated by construction instead of by its
        # author remembering to. The extractors above ALSO refuse to fabricate,
        # which is not redundancy — they catch what a band cannot, namely a
        # value that is unmeasured yet physically legal (an unset 0.0 jitter
        # sits squarely inside its band and would sail through this gate).
        measure = _VALIDATOR.coerce
        features = VoiceBiometricFeatures(
            embedding=embedding,
            embedding_confidence=0.9,
            pitch_mean=measure('pitch_mean_hz', pitch_features['mean']),
            pitch_std=measure('pitch_std_hz', pitch_features['std']),
            pitch_range=measure('pitch_range_hz', pitch_features['range']),
            formant_f1=measure('formant_f1_hz', formants[0]),
            formant_f2=measure('formant_f2_hz', formants[1]),
            formant_f3=measure('formant_f3_hz', formants[2]),
            formant_f4=measure('formant_f4_hz', formants[3]),
            spectral_centroid=measure('spectral_centroid_hz', spectral_features['centroid']),
            spectral_rolloff=measure('spectral_rolloff_hz', spectral_features['rolloff']),
            spectral_flux=spectral_features['flux'],
            spectral_entropy=spectral_features['entropy'],
            speaking_rate=measure('speaking_rate_wpm', temporal_features['rate']),
            pause_ratio=measure('pause_ratio', temporal_features['pause_ratio']),
            energy_contour=temporal_features['energy'],
            jitter=measure('jitter', quality_features['jitter']),
            shimmer=measure('shimmer', quality_features['shimmer']),
            harmonic_to_noise_ratio=measure('harmonic_to_noise_ratio_db', quality_features['hnr']),
            duration_seconds=len(audio_np) / self.sample_rate,
            sample_rate=self.sample_rate
        )

        return features

    # =========================================================================
    # ASYNC WRAPPERS - Run CPU-intensive work in thread pool
    # =========================================================================

    async def _extract_pitch_features_async(self, audio: np.ndarray) -> dict:
        """Async wrapper for pitch extraction."""
        return await self._run_in_executor(self._extract_pitch_features_sync, audio)

    async def _extract_formants_async(self, audio: np.ndarray) -> list:
        """Async wrapper for formant extraction."""
        return await self._run_in_executor(self._extract_formants_sync, audio)

    async def _extract_spectral_features_async(self, audio: np.ndarray) -> dict:
        """Async wrapper for spectral feature extraction."""
        return await self._run_in_executor(self._extract_spectral_features_sync, audio)

    async def _extract_temporal_features_async(self, audio: np.ndarray, transcription: str) -> dict:
        """Async wrapper for temporal feature extraction."""
        return await self._run_in_executor(self._extract_temporal_features_sync, audio, transcription)

    async def _extract_voice_quality_async(self, audio: np.ndarray) -> dict:
        """Async wrapper for voice quality extraction."""
        return await self._run_in_executor(self._extract_voice_quality_sync, audio)

    # =========================================================================
    # SYNC IMPLEMENTATIONS - Run in thread pool
    # =========================================================================

    def _extract_pitch_features_sync(self, audio: np.ndarray) -> dict:
        """Extract pitch features using autocorrelation (CPU-intensive)."""
        frame_size = 2048
        hop_size = 512
        pitches = []

        for i in range(0, len(audio) - frame_size, hop_size):
            frame = audio[i:i + frame_size]

            # Autocorrelation
            correlation = np.correlate(frame, frame, mode='full')
            correlation = correlation[len(correlation) // 2:]

            # Peak detection
            min_lag = int(self.sample_rate / 500)
            max_lag = int(self.sample_rate / 50)

            if max_lag < len(correlation):
                search_region = correlation[min_lag:max_lag]
                if len(search_region) > 0 and correlation[0] > 0:
                    peak_lag = min_lag + np.argmax(search_region)
                    if correlation[peak_lag] > 0.3 * correlation[0]:
                        pitch = self.sample_rate / peak_lag
                        if 50 <= pitch <= 500:
                            pitches.append(pitch)

        if pitches:
            return {
                'mean': float(np.mean(pitches)),
                'std': float(np.std(pitches)),
                'range': float(np.max(pitches) - np.min(pitches))
            }

        # No frame produced a periodic peak: this recording has no trackable f0.
        # It used to return {'mean': 150.0, 'std': 20.0, 'range': 50.0} — an
        # invented average male voice, indistinguishable downstream from a
        # measured one, and scored against the enrolled speaker as if real.
        logger.warning(
            "[Features] no voiced frame yielded a pitch period across %d "
            "samples — reporting pitch unmeasured", len(audio),
        )
        return {'mean': UNMEASURED, 'std': UNMEASURED, 'range': UNMEASURED}

    def _extract_formants_sync(self, audio: np.ndarray) -> list:
        """
        Extract formant frequencies using LPC (CPU-intensive).

        This function used to claim LPC and perform spectral peak-picking: it
        took ``find_peaks`` over the whole-utterance magnitude spectrum and
        returned ``freqs[peaks[:4]]`` — the four LOWEST peaks by frequency,
        which in any real recording are DC offset, mains hum and f0 harmonics.
        Its sibling in ``enroll_voice`` did the same and wrote the result to
        this machine's profile: F1 = 42.9 Hz, F2 = 80.0 Hz.

        Both are now the one estimator in ``voice/formant_estimation``, which
        fits an all-pole model per voiced frame and reads the pole angles. The
        constants that used to be returned on failure are gone: an unresolvable
        formant is NaN, because ``[500, 1500, 2500, 3500]`` is the textbook male
        average and writing it into a specific speaker's profile as though it
        had been measured is the defect, not the mitigation.
        """
        return estimate_formants(audio, self.sample_rate, n_formants=4)

    def _extract_spectral_features_sync(self, audio: np.ndarray) -> dict:
        """Extract spectral features (CPU-intensive FFT operations)."""
        fft = np.fft.rfft(audio)
        magnitude = np.abs(fft)
        power = magnitude ** 2
        freqs = np.fft.rfftfreq(len(audio), 1 / self.sample_rate)

        # Spectral centroid
        centroid = float(np.sum(freqs * magnitude) / (np.sum(magnitude) + 1e-10))

        # Spectral rolloff
        cumsum = np.cumsum(power)
        rolloff_threshold = 0.85 * cumsum[-1]
        rolloff_idx = np.where(cumsum >= rolloff_threshold)[0]
        rolloff = float(freqs[rolloff_idx[0]] if len(rolloff_idx) > 0 else freqs[-1])

        # Spectral flux
        flux = float(np.std(magnitude))

        # Spectral entropy
        power_norm = power / (np.sum(power) + 1e-10)
        entropy = float(-np.sum(power_norm * np.log2(power_norm + 1e-10)))

        return {
            'centroid': centroid,
            'rolloff': rolloff,
            'flux': flux,
            'entropy': entropy
        }

    def _extract_temporal_features_sync(self, audio: np.ndarray, transcription: str) -> dict:
        """Extract temporal features (CPU-intensive)."""
        duration = len(audio) / self.sample_rate

        # Speaking rate (words per minute).
        #
        # This is where 420 wpm came from. Two ways, both of which used to be
        # reported as a rate:
        #
        #   * No transcription. ``word_count`` is 0, the rate is 0.0 wpm, and a
        #     speaker who said nothing is stored as one who speaks at zero words
        #     per minute — then differenced against the live speaker and found
        #     to be inconsistent. Callers that pass ``transcription=""`` (the
        #     acoustic-only path in ``speaker_verification_service``) hit this
        #     on every single sample.
        #   * A short clip. Words counted over a window trimmed to the speech
        #     segment divides by a duration far smaller than the utterance took,
        #     and 7 words over 1.0 s is exactly 420 wpm.
        #
        # Neither is a measurement of how fast this person speaks, so neither is
        # reported as one. The band check catches the second case; only the
        # explicit absence check can catch the first, because 0.0 is a perfectly
        # well-formed float that no downstream consumer can distinguish from a
        # measurement.
        word_count = len(transcription.split()) if transcription else 0
        if word_count == 0 or duration <= 0:
            logger.debug(
                "[Features] speaking rate needs words and a duration "
                "(words=%d, duration=%.2fs) — reporting unmeasured",
                word_count, duration,
            )
            speaking_rate = UNMEASURED
        else:
            speaking_rate = _VALIDATOR.coerce(
                "speaking_rate_wpm", (word_count / duration) * 60)

        # Energy contour (frame-based)
        frame_size = self.sample_rate // 20
        num_frames = len(audio) // frame_size

        energy_contour = []
        pauses = 0
        mean_energy = np.mean(audio ** 2)

        for i in range(num_frames):
            frame = audio[i * frame_size:(i + 1) * frame_size]
            energy = np.sum(frame ** 2)
            energy_contour.append(energy)

            if energy < mean_energy * 0.05:
                pauses += 1

        pause_ratio = pauses / max(num_frames, 1)

        return {
            'rate': float(speaking_rate),
            'pause_ratio': float(pause_ratio),
            'energy': np.array(energy_contour)
        }

    def _extract_voice_quality_sync(self, audio: np.ndarray) -> dict:
        """Extract voice quality metrics (CPU-intensive).

        IMPORTANT: For long audio, we sample a representative chunk to avoid
        O(n²) autocorrelation complexity that would hang for 30+ seconds.
        """
        # Limit audio length for voice quality analysis (max 3 seconds)
        # Voice quality metrics don't need the entire audio
        max_samples = int(self.sample_rate * 3)  # 3 seconds max
        if len(audio) > max_samples:
            # Take middle portion for best representation
            start = (len(audio) - max_samples) // 2
            audio_chunk = audio[start:start + max_samples]
        else:
            audio_chunk = audio

        jitter = self._compute_jitter_sync(audio_chunk)
        shimmer = self._compute_shimmer_sync(audio_chunk)
        hnr = self._compute_hnr_sync(audio_chunk)

        return {
            'jitter': float(jitter),
            'shimmer': float(shimmer),
            'hnr': float(hnr)
        }

    def _compute_jitter_sync(self, audio: np.ndarray) -> float:
        """Compute jitter (pitch period variation)."""
        try:
            frame_size = 2048
            hop_size = 512
            periods = []

            for i in range(0, len(audio) - frame_size, hop_size):
                frame = audio[i:i + frame_size]
                correlation = np.correlate(frame, frame, mode='full')
                correlation = correlation[len(correlation) // 2:]

                min_lag = int(self.sample_rate / 500)
                max_lag = int(self.sample_rate / 50)

                if max_lag < len(correlation) and correlation[0] > 0:
                    search_region = correlation[min_lag:max_lag]
                    if len(search_region) > 0:
                        peak_lag = min_lag + np.argmax(search_region)
                        if correlation[peak_lag] > 0.3 * correlation[0]:
                            period = peak_lag / self.sample_rate
                            periods.append(period)

            if len(periods) > 1:
                diffs = np.abs(np.diff(periods))
                jitter = np.mean(diffs) / np.mean(periods)
                return float(min(jitter, 0.1))

            # Jitter is a cycle-TO-CYCLE ratio; with fewer than two periods
            # there is no pair to compare. The 0.01 this used to return is a
            # healthy-voice value, so an unanalysable recording scored as a
            # clean one — and unlike the formants, 0.01 is inside the plausible
            # band, so no downstream gate can catch it. Only the extractor can.
            logger.warning(
                "[Features] jitter needs 2+ pitch periods, found %d — "
                "reporting unmeasured", len(periods),
            )
            return UNMEASURED

        except Exception as exc:  # noqa: BLE001 — a failed measurement is not a value
            logger.warning("[Features] jitter computation failed (%s: %s) — "
                           "reporting unmeasured", type(exc).__name__, exc)
            return UNMEASURED

    def _compute_shimmer_sync(self, audio: np.ndarray) -> float:
        """Compute shimmer (amplitude variation)."""
        try:
            frame_size = int(self.sample_rate * 0.01)  # 10ms frames
            num_frames = len(audio) // frame_size

            peaks = []
            for i in range(num_frames):
                frame = audio[i * frame_size:(i + 1) * frame_size]
                peak = np.max(np.abs(frame))
                peaks.append(peak)

            if len(peaks) > 1:
                mean_peak = float(np.mean(peaks))
                if mean_peak <= 0:
                    # Every frame peaked at zero: silence, not a steady voice.
                    # The +1e-10 guard below used to turn this into shimmer 0.0,
                    # a perfect score for a recording with no signal in it.
                    logger.warning("[Features] shimmer: all frames are silent — "
                                   "reporting unmeasured")
                    return UNMEASURED
                diffs = np.abs(np.diff(peaks))
                shimmer = np.mean(diffs) / mean_peak
                return float(min(shimmer, 0.5))

            logger.warning(
                "[Features] shimmer needs 2+ frames, found %d — reporting "
                "unmeasured", len(peaks),
            )
            return UNMEASURED

        except Exception as exc:  # noqa: BLE001 — a failed measurement is not a value
            logger.warning("[Features] shimmer computation failed (%s: %s) — "
                           "reporting unmeasured", type(exc).__name__, exc)
            return UNMEASURED

    def _compute_hnr_sync(self, audio: np.ndarray) -> float:
        """Compute Harmonic-to-Noise Ratio.

        Uses a single representative frame instead of full-length autocorrelation
        to avoid O(n²) complexity that would hang on long audio.
        """
        try:
            # Use a single frame for HNR (much faster than full-length autocorrelation)
            # Take a frame from the middle of the audio for best representation
            frame_size = min(4096, len(audio))
            start = max(0, (len(audio) - frame_size) // 2)
            frame = audio[start:start + frame_size]

            correlation = np.correlate(frame, frame, mode='full')
            correlation = correlation[len(correlation) // 2:]

            min_lag = int(self.sample_rate / 500)
            max_lag = int(self.sample_rate / 50)

            if max_lag < len(correlation) and correlation[0] > 0:
                search_region = correlation[min_lag:max_lag]
                if len(search_region) > 0:
                    peak_idx = min_lag + np.argmax(search_region)
                    peak_value = correlation[peak_idx]

                    noise_floor = np.median(correlation[min_lag:max_lag])
                    if noise_floor <= 0:
                        logger.warning("[Features] HNR: no positive noise floor "
                                       "— reporting unmeasured")
                        return UNMEASURED
                    hnr_linear = peak_value / noise_floor
                    if hnr_linear <= 0:
                        return UNMEASURED
                    hnr_db = 10 * np.log10(hnr_linear)

                    # NaN here means the arithmetic could not be completed — it
                    # used to become 15.0, a healthy-voice HNR, on exactly the
                    # noise-only audio the comment names. Keep it as NaN.
                    if not np.isfinite(hnr_db):
                        logger.warning("[Features] HNR is not finite — "
                                       "reporting unmeasured")
                        return UNMEASURED

                    # Clipped to the measurable band rather than to [0, 40]: the
                    # old lower clip at 0.0 silently converted every negative
                    # HNR (noise louder than harmonics — a real, informative
                    # reading) into the boundary value.
                    return float(np.clip(hnr_db, _HNR_BAND.low, _HNR_BAND.high))

            logger.warning(
                "[Features] HNR: no usable autocorrelation lag in %d samples — "
                "reporting unmeasured", len(audio),
            )
            return UNMEASURED

        except Exception as exc:  # noqa: BLE001 — a failed measurement is not a value
            logger.warning("[Features] HNR computation failed (%s: %s) — "
                           "reporting unmeasured", type(exc).__name__, exc)
            return UNMEASURED


def shutdown_feature_executor():
    """Shutdown the feature extraction thread pool gracefully."""
    global _feature_executor
    if _feature_executor is not None:
        logger.info("🔧 Shutting down feature extraction thread pool...")
        _feature_executor.shutdown(wait=True)
        _feature_executor = None
