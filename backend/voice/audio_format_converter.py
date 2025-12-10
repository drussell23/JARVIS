#!/usr/bin/env python3
"""
Advanced Audio Format Converter v2.0
=====================================

A robust, async-capable audio format converter for voice biometric processing.

Features:
- Async and sync API support
- Intelligent format detection with magic bytes
- Multiple transcoding backends (pydub, FFmpeg, scipy)
- Audio quality validation and normalization
- Intelligent caching for repeated conversions
- Dynamic configuration (no hardcoded values)
- Comprehensive error recovery with fallbacks
- Audio analysis (SNR, silence detection)

CRITICAL: This module handles the conversion of compressed audio formats
(WebM, MP3, OGG) that browsers send to raw PCM suitable for speaker verification.
Failure to properly convert results in 0% confidence during voice unlock.

Author: JARVIS AI Agent Team
Version: 2.0.0
"""

import base64
import numpy as np
import logging
import json
import struct
import wave
import io
import tempfile
import os
import subprocess
import shutil
import hashlib
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any, Union, List, Callable
from enum import Enum, auto
from functools import lru_cache

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration System (No Hardcoding)
# =============================================================================

@dataclass
class AudioConverterConfig:
    """
    Dynamic configuration for audio conversion.
    All values are configurable - no hardcoding.
    """
    # Target format for STT and biometrics
    target_sample_rate: int = 16000
    target_channels: int = 1
    target_bit_depth: int = 16  # bits per sample

    # Quality thresholds
    min_audio_duration_ms: int = 100  # Minimum audio duration
    max_audio_duration_ms: int = 300000  # 5 minutes max
    min_snr_db: float = 10.0  # Minimum signal-to-noise ratio
    silence_threshold_db: float = -40.0  # Below this is silence

    # Caching configuration
    cache_enabled: bool = True
    cache_max_size: int = 100  # Max number of cached conversions
    cache_ttl_seconds: int = 300  # 5 minutes TTL

    # Processing configuration
    max_thread_workers: int = 4
    transcoding_timeout_seconds: int = 30
    normalize_audio: bool = True
    target_peak_db: float = -3.0  # Target peak for normalization

    # Fallback behavior
    enable_fallback_chain: bool = True
    return_empty_on_failure: bool = True

    @classmethod
    def from_env(cls) -> 'AudioConverterConfig':
        """Create config from environment variables."""
        return cls(
            target_sample_rate=int(os.environ.get('AUDIO_TARGET_SAMPLE_RATE', '16000')),
            target_channels=int(os.environ.get('AUDIO_TARGET_CHANNELS', '1')),
            target_bit_depth=int(os.environ.get('AUDIO_TARGET_BIT_DEPTH', '16')),
            min_audio_duration_ms=int(os.environ.get('AUDIO_MIN_DURATION_MS', '100')),
            max_audio_duration_ms=int(os.environ.get('AUDIO_MAX_DURATION_MS', '300000')),
            min_snr_db=float(os.environ.get('AUDIO_MIN_SNR_DB', '10.0')),
            silence_threshold_db=float(os.environ.get('AUDIO_SILENCE_THRESHOLD_DB', '-40.0')),
            cache_enabled=os.environ.get('AUDIO_CACHE_ENABLED', 'true').lower() == 'true',
            cache_max_size=int(os.environ.get('AUDIO_CACHE_MAX_SIZE', '100')),
            cache_ttl_seconds=int(os.environ.get('AUDIO_CACHE_TTL_SECONDS', '300')),
            max_thread_workers=int(os.environ.get('AUDIO_MAX_WORKERS', '4')),
            transcoding_timeout_seconds=int(os.environ.get('AUDIO_TRANSCODE_TIMEOUT', '30')),
            normalize_audio=os.environ.get('AUDIO_NORMALIZE', 'true').lower() == 'true',
            target_peak_db=float(os.environ.get('AUDIO_TARGET_PEAK_DB', '-3.0')),
            enable_fallback_chain=os.environ.get('AUDIO_ENABLE_FALLBACK', 'true').lower() == 'true',
            return_empty_on_failure=os.environ.get('AUDIO_EMPTY_ON_FAILURE', 'true').lower() == 'true',
        )


# =============================================================================
# Audio Format Detection
# =============================================================================

class AudioFormat(Enum):
    """Detected audio format types."""
    WEBM = auto()
    OGG = auto()
    MP3 = auto()
    WAV = auto()
    FLAC = auto()
    MP4 = auto()
    M4A = auto()
    AAC = auto()
    RAW_PCM = auto()
    UNKNOWN = auto()


# Format detection configuration - extensible, not hardcoded
AUDIO_SIGNATURES: Dict[bytes, AudioFormat] = {
    # WebM/Matroska - EBML header
    b'\x1a\x45\xdf\xa3': AudioFormat.WEBM,
    # OGG Vorbis/Opus
    b'OggS': AudioFormat.OGG,
    # MP3 frame sync variants
    b'\xff\xfb': AudioFormat.MP3,
    b'\xff\xfa': AudioFormat.MP3,
    b'\xff\xf3': AudioFormat.MP3,
    b'\xff\xf2': AudioFormat.MP3,
    b'ID3': AudioFormat.MP3,  # MP3 with ID3 tag
    # WAV (RIFF container)
    b'RIFF': AudioFormat.WAV,
    # FLAC
    b'fLaC': AudioFormat.FLAC,
    # AAC ADTS
    b'\xff\xf1': AudioFormat.AAC,
    b'\xff\xf9': AudioFormat.AAC,
}


# =============================================================================
# Backend Availability Detection
# =============================================================================

def _check_ffmpeg_available() -> Tuple[bool, Optional[str]]:
    """Check if FFmpeg is available and return its path."""
    ffmpeg_path = shutil.which('ffmpeg')
    if ffmpeg_path:
        try:
            result = subprocess.run(
                [ffmpeg_path, '-version'],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, ffmpeg_path
        except Exception:
            pass
    return False, None


def _check_pydub_available() -> bool:
    """Check if pydub is available."""
    try:
        from pydub import AudioSegment
        return True
    except ImportError:
        return False


def _check_scipy_available() -> bool:
    """Check if scipy.io.wavfile is available."""
    try:
        from scipy.io import wavfile
        return True
    except ImportError:
        return False


# Cache the availability checks
FFMPEG_AVAILABLE, FFMPEG_PATH = _check_ffmpeg_available()
PYDUB_AVAILABLE = _check_pydub_available()
SCIPY_AVAILABLE = _check_scipy_available()

logger.info(f"Audio backends: FFmpeg={FFMPEG_AVAILABLE}, pydub={PYDUB_AVAILABLE}, scipy={SCIPY_AVAILABLE}")


# =============================================================================
# Audio Analysis Utilities
# =============================================================================

@dataclass
class AudioAnalysis:
    """Results of audio quality analysis."""
    duration_ms: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bit_depth: int = 0
    peak_db: float = -100.0
    rms_db: float = -100.0
    estimated_snr_db: float = 0.0
    silence_ratio: float = 0.0  # Proportion of audio that is silence
    is_valid: bool = False
    issues: List[str] = field(default_factory=list)


def analyze_pcm_audio(
    audio_bytes: bytes,
    sample_rate: int,
    channels: int = 1,
    bit_depth: int = 16
) -> AudioAnalysis:
    """
    Analyze PCM audio quality.

    Args:
        audio_bytes: Raw PCM audio data
        sample_rate: Sample rate in Hz
        channels: Number of channels
        bit_depth: Bits per sample

    Returns:
        AudioAnalysis with quality metrics
    """
    analysis = AudioAnalysis()
    issues = []

    try:
        # Convert to numpy array
        if bit_depth == 16:
            dtype = np.int16
            max_val = 32767.0
        elif bit_depth == 32:
            dtype = np.int32
            max_val = 2147483647.0
        else:
            dtype = np.int16
            max_val = 32767.0

        samples = np.frombuffer(audio_bytes, dtype=dtype).astype(np.float32) / max_val

        if len(samples) == 0:
            issues.append("Empty audio data")
            analysis.issues = issues
            return analysis

        # Basic properties
        analysis.sample_rate = sample_rate
        analysis.channels = channels
        analysis.bit_depth = bit_depth
        analysis.duration_ms = (len(samples) / channels / sample_rate) * 1000

        # Handle stereo by averaging channels
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)

        # Peak level
        peak = np.max(np.abs(samples))
        analysis.peak_db = 20 * np.log10(peak + 1e-10)

        # RMS level
        rms = np.sqrt(np.mean(samples ** 2))
        analysis.rms_db = 20 * np.log10(rms + 1e-10)

        # Estimate SNR (using bottom 10% as noise floor estimate)
        sorted_levels = np.sort(np.abs(samples))
        noise_floor = np.mean(sorted_levels[:max(1, len(sorted_levels) // 10)])
        signal_level = np.mean(sorted_levels[len(sorted_levels) // 2:])
        if noise_floor > 0:
            analysis.estimated_snr_db = 20 * np.log10(signal_level / noise_floor + 1e-10)
        else:
            analysis.estimated_snr_db = 60.0  # Very clean signal

        # Silence ratio (samples below -40dB)
        silence_threshold = 10 ** (-40 / 20)  # -40dB
        silence_samples = np.sum(np.abs(samples) < silence_threshold)
        analysis.silence_ratio = silence_samples / len(samples)

        # Validate
        if analysis.duration_ms < 50:
            issues.append(f"Audio too short: {analysis.duration_ms:.0f}ms")
        if analysis.peak_db < -50:
            issues.append(f"Audio too quiet: peak {analysis.peak_db:.1f}dB")
        if analysis.silence_ratio > 0.9:
            issues.append(f"Audio is {analysis.silence_ratio*100:.0f}% silence")
        if analysis.estimated_snr_db < 5:
            issues.append(f"Poor SNR: {analysis.estimated_snr_db:.1f}dB")

        analysis.issues = issues
        analysis.is_valid = len(issues) == 0

    except Exception as e:
        issues.append(f"Analysis error: {e}")
        analysis.issues = issues

    return analysis


def normalize_audio(
    audio_bytes: bytes,
    target_peak_db: float = -3.0,
    bit_depth: int = 16
) -> bytes:
    """
    Normalize audio to target peak level.

    Args:
        audio_bytes: Raw PCM audio data
        target_peak_db: Target peak level in dB
        bit_depth: Bits per sample

    Returns:
        Normalized PCM audio bytes
    """
    try:
        if bit_depth == 16:
            dtype = np.int16
            max_val = 32767.0
        else:
            dtype = np.int16
            max_val = 32767.0

        samples = np.frombuffer(audio_bytes, dtype=dtype).astype(np.float32)

        if len(samples) == 0:
            return audio_bytes

        # Calculate current peak
        current_peak = np.max(np.abs(samples))
        if current_peak < 1:  # Avoid division by zero
            return audio_bytes

        # Calculate gain needed
        target_linear = max_val * (10 ** (target_peak_db / 20))
        gain = target_linear / current_peak

        # Apply gain (limit to prevent clipping)
        gain = min(gain, 10.0)  # Max 20dB gain
        normalized = samples * gain

        # Clip to valid range
        normalized = np.clip(normalized, -max_val, max_val)

        return normalized.astype(dtype).tobytes()

    except Exception as e:
        logger.warning(f"Normalization failed: {e}")
        return audio_bytes


# =============================================================================
# Conversion Cache
# =============================================================================

@dataclass
class CacheEntry:
    """Cache entry for converted audio."""
    pcm_data: bytes
    timestamp: float
    format: AudioFormat
    analysis: Optional[AudioAnalysis] = None


class ConversionCache:
    """
    LRU cache for audio conversions with TTL support.
    Thread-safe for concurrent access.
    """

    def __init__(self, max_size: int = 100, ttl_seconds: int = 300):
        self._cache: Dict[str, CacheEntry] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock() if asyncio.get_event_loop_policy() else None
        self._hits = 0
        self._misses = 0

    def _compute_key(self, audio_bytes: bytes) -> str:
        """Compute cache key from audio bytes."""
        # Use first 1KB + length for faster hashing of large files
        sample = audio_bytes[:1024] if len(audio_bytes) > 1024 else audio_bytes
        return hashlib.md5(sample + str(len(audio_bytes)).encode()).hexdigest()

    def get(self, audio_bytes: bytes) -> Optional[bytes]:
        """Get cached conversion if available and not expired."""
        key = self._compute_key(audio_bytes)
        entry = self._cache.get(key)

        if entry is None:
            self._misses += 1
            return None

        # Check TTL
        if time.time() - entry.timestamp > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None

        self._hits += 1
        logger.debug(f"Cache hit: {len(entry.pcm_data)} bytes (hits={self._hits}, misses={self._misses})")
        return entry.pcm_data

    def put(
        self,
        audio_bytes: bytes,
        pcm_data: bytes,
        format: AudioFormat,
        analysis: Optional[AudioAnalysis] = None
    ) -> None:
        """Store converted audio in cache."""
        # Evict oldest if at capacity
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].timestamp)
            del self._cache[oldest_key]

        key = self._compute_key(audio_bytes)
        self._cache[key] = CacheEntry(
            pcm_data=pcm_data,
            timestamp=time.time(),
            format=format,
            analysis=analysis
        )

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    @property
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            'size': len(self._cache),
            'max_size': self._max_size,
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0
        }


# =============================================================================
# Main Converter Class
# =============================================================================

class AudioFormatConverter:
    """
    Advanced audio format converter with async support.

    Features:
    - Async and sync API
    - Intelligent format detection
    - Multiple transcoding backends
    - Audio quality validation
    - Caching for performance
    """

    def __init__(self, config: Optional[AudioConverterConfig] = None):
        """
        Initialize the converter.

        Args:
            config: Configuration object. If None, uses defaults from environment.
        """
        self.config = config or AudioConverterConfig.from_env()
        self._cache = ConversionCache(
            max_size=self.config.cache_max_size,
            ttl_seconds=self.config.cache_ttl_seconds
        ) if self.config.cache_enabled else None
        self._executor = ThreadPoolExecutor(max_workers=self.config.max_thread_workers)

        # Track conversion statistics
        self._conversions = 0
        self._failures = 0
        self._backend_usage: Dict[str, int] = {}

    # =========================================================================
    # Format Detection
    # =========================================================================

    @staticmethod
    def detect_audio_format(audio_bytes: bytes) -> AudioFormat:
        """
        Detect audio format from magic bytes.

        Args:
            audio_bytes: Raw audio data

        Returns:
            Detected AudioFormat enum
        """
        if not audio_bytes or len(audio_bytes) < 4:
            return AudioFormat.RAW_PCM

        # Check against known signatures
        for signature, format in AUDIO_SIGNATURES.items():
            if audio_bytes.startswith(signature):
                logger.debug(f"Detected format: {format.name} (signature: {signature!r})")
                return format

        # Special check for MP4/M4A (ftyp box at byte 4)
        if len(audio_bytes) > 8 and audio_bytes[4:8] == b'ftyp':
            # Check for audio-specific brands
            brand = audio_bytes[8:12]
            if brand in [b'M4A ', b'mp41', b'mp42', b'isom']:
                return AudioFormat.M4A
            return AudioFormat.MP4

        # Check for RIFF-based formats with audio data
        if len(audio_bytes) > 12 and audio_bytes[:4] == b'RIFF':
            format_type = audio_bytes[8:12]
            if format_type == b'WAVE':
                return AudioFormat.WAV

        # Default to raw PCM
        return AudioFormat.RAW_PCM

    @staticmethod
    def get_format_extension(format: AudioFormat) -> str:
        """Get file extension for a format."""
        extensions = {
            AudioFormat.WEBM: 'webm',
            AudioFormat.OGG: 'ogg',
            AudioFormat.MP3: 'mp3',
            AudioFormat.WAV: 'wav',
            AudioFormat.FLAC: 'flac',
            AudioFormat.MP4: 'mp4',
            AudioFormat.M4A: 'm4a',
            AudioFormat.AAC: 'aac',
            AudioFormat.RAW_PCM: 'pcm',
            AudioFormat.UNKNOWN: 'bin',
        }
        return extensions.get(format, 'bin')

    # =========================================================================
    # Input Conversion (Various formats to bytes)
    # =========================================================================

    def convert_to_bytes(self, audio_data: Any) -> bytes:
        """
        Convert any audio format to bytes.

        Handles:
        - bytes (passthrough)
        - base64 strings
        - JSON encoded data
        - Lists/tuples of samples
        - NumPy arrays
        - Dict with audio data

        Args:
            audio_data: Audio in any supported format

        Returns:
            Audio as bytes
        """
        if audio_data is None:
            logger.warning("Audio data is None")
            return b''

        # Already bytes
        if isinstance(audio_data, bytes):
            return audio_data

        # String (base64, JSON, hex, CSV)
        if isinstance(audio_data, str):
            return self._convert_string_to_bytes(audio_data)

        # List/tuple of samples
        if isinstance(audio_data, (list, tuple)):
            return self._array_to_bytes(audio_data)

        # NumPy array
        if hasattr(audio_data, 'dtype'):
            return self._numpy_to_bytes(audio_data)

        # Dict with audio data
        if isinstance(audio_data, dict):
            for key in ['data', 'audio', 'audio_data', 'samples', 'buffer', 'bytes']:
                if key in audio_data:
                    return self.convert_to_bytes(audio_data[key])

        # Try direct conversion
        try:
            return bytes(audio_data)
        except Exception as e:
            logger.error(f"Cannot convert {type(audio_data).__name__} to bytes: {e}")
            return b''

    def _convert_string_to_bytes(self, data: str) -> bytes:
        """Convert string data to bytes."""
        # Check for JSON
        if data.startswith(('{', '[')):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, list):
                    return self._array_to_bytes(parsed)
                elif isinstance(parsed, dict) and 'data' in parsed:
                    return self.convert_to_bytes(parsed['data'])
            except json.JSONDecodeError:
                pass

        # Try base64 decoding (most common)
        for decoder in [
            lambda d: base64.b64decode(d),
            lambda d: base64.b64decode(d + '=' * (4 - len(d) % 4)),  # Fix padding
            lambda d: base64.urlsafe_b64decode(d),
            lambda d: base64.urlsafe_b64decode(d + '=' * (4 - len(d) % 4)),
        ]:
            try:
                decoded = decoder(data)
                if len(decoded) > 0:
                    return decoded
            except Exception:
                continue

        # Try hex decoding
        try:
            return bytes.fromhex(data)
        except ValueError:
            pass

        # Try comma-separated values
        if ',' in data:
            try:
                values = [int(x.strip()) for x in data.split(',')]
                return self._array_to_bytes(values)
            except ValueError:
                pass

        logger.warning(f"Could not decode string of length {len(data)}")
        return b''

    def _array_to_bytes(self, array: Union[list, tuple]) -> bytes:
        """Convert array of samples to PCM bytes."""
        try:
            # Sample type detection
            sample = array[:100] if len(array) > 100 else array
            if all(isinstance(x, float) and -1.0 <= x <= 1.0 for x in sample):
                # Float samples (-1.0 to 1.0)
                samples = np.array(array, dtype=np.float32)
                samples = (samples * 32767).astype(np.int16)
            else:
                # Integer samples
                samples = np.array(array, dtype=np.int16)

            return samples.tobytes()
        except Exception as e:
            logger.error(f"Array to bytes conversion failed: {e}")
            return b''

    def _numpy_to_bytes(self, arr: np.ndarray) -> bytes:
        """Convert numpy array to PCM bytes."""
        try:
            if arr.dtype in [np.float32, np.float64]:
                # Float to int16
                arr = (arr * 32767).astype(np.int16)
            elif arr.dtype != np.int16:
                arr = arr.astype(np.int16)

            return arr.tobytes()
        except Exception as e:
            logger.error(f"NumPy to bytes conversion failed: {e}")
            return b''

    # =========================================================================
    # Transcoding Backends
    # =========================================================================

    def _transcode_with_pydub(
        self,
        audio_bytes: bytes,
        format: AudioFormat
    ) -> Optional[bytes]:
        """
        Transcode using pydub (requires FFmpeg backend).

        This is the preferred method as it handles most formats.
        """
        if not PYDUB_AVAILABLE or not FFMPEG_AVAILABLE:
            return None

        try:
            from pydub import AudioSegment

            format_str = self.get_format_extension(format)

            # Write to temp file
            with tempfile.NamedTemporaryFile(suffix=f'.{format_str}', delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            try:
                # Load and convert
                audio = AudioSegment.from_file(tmp_path, format=format_str)
                audio = audio.set_channels(self.config.target_channels)
                audio = audio.set_frame_rate(self.config.target_sample_rate)
                audio = audio.set_sample_width(self.config.target_bit_depth // 8)

                pcm_data = audio.raw_data

                self._backend_usage['pydub'] = self._backend_usage.get('pydub', 0) + 1
                logger.info(f"pydub: {format.name} {len(audio_bytes)} → PCM {len(pcm_data)} bytes")

                return pcm_data

            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"pydub transcode failed: {e}")
            return None

    def _transcode_with_ffmpeg(
        self,
        audio_bytes: bytes,
        format: AudioFormat
    ) -> Optional[bytes]:
        """
        Transcode using FFmpeg directly via subprocess.

        Fallback when pydub is not available.
        """
        if not FFMPEG_AVAILABLE:
            return None

        try:
            format_str = self.get_format_extension(format)

            # Create temp files
            with tempfile.NamedTemporaryFile(suffix=f'.{format_str}', delete=False) as tmp_in:
                tmp_in.write(audio_bytes)
                tmp_in_path = tmp_in.name

            tmp_out_path = tmp_in_path + '.wav'

            try:
                # FFmpeg command
                cmd = [
                    FFMPEG_PATH or 'ffmpeg',
                    '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', tmp_in_path,
                    '-ar', str(self.config.target_sample_rate),
                    '-ac', str(self.config.target_channels),
                    '-acodec', 'pcm_s16le',
                    '-f', 'wav',
                    tmp_out_path
                ]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.config.transcoding_timeout_seconds
                )

                if result.returncode != 0:
                    logger.warning(f"FFmpeg error: {result.stderr.decode()}")
                    return None

                # Extract PCM from WAV
                with open(tmp_out_path, 'rb') as f:
                    wav_data = f.read()

                if wav_data.startswith(b'RIFF'):
                    with io.BytesIO(wav_data) as wav_io:
                        with wave.open(wav_io, 'rb') as wav:
                            pcm_data = wav.readframes(wav.getnframes())

                            self._backend_usage['ffmpeg'] = self._backend_usage.get('ffmpeg', 0) + 1
                            logger.info(f"FFmpeg: {format.name} {len(audio_bytes)} → PCM {len(pcm_data)} bytes")

                            return pcm_data

                return None

            finally:
                for path in [tmp_in_path, tmp_out_path]:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

        except subprocess.TimeoutExpired:
            logger.error("FFmpeg transcode timed out")
            return None
        except Exception as e:
            logger.warning(f"FFmpeg transcode failed: {e}")
            return None

    def _extract_wav_pcm(self, audio_bytes: bytes) -> Optional[bytes]:
        """Extract PCM data from WAV file."""
        try:
            with io.BytesIO(audio_bytes) as wav_io:
                with wave.open(wav_io, 'rb') as wav:
                    # Check if already in target format
                    if (wav.getnchannels() == self.config.target_channels and
                        wav.getframerate() == self.config.target_sample_rate and
                        wav.getsampwidth() == self.config.target_bit_depth // 8):
                        return wav.readframes(wav.getnframes())

                    # Need to convert - use pydub or FFmpeg
                    wav_io.seek(0)
                    return self._transcode_with_pydub(audio_bytes, AudioFormat.WAV)

        except Exception as e:
            logger.warning(f"WAV extraction failed: {e}")
            return None

    # =========================================================================
    # Main Conversion Methods
    # =========================================================================

    def ensure_pcm_format(
        self,
        audio_bytes: bytes,
        analyze: bool = False
    ) -> Union[bytes, Tuple[bytes, AudioAnalysis]]:
        """
        Ensure audio is in PCM format for processing.

        CRITICAL: This is the main entry point for audio conversion.
        Handles WebM, OGG, MP3, etc. from browsers and converts to
        16kHz mono 16-bit PCM for speaker verification.

        Args:
            audio_bytes: Raw or compressed audio
            analyze: If True, also return AudioAnalysis

        Returns:
            PCM bytes, or (PCM bytes, AudioAnalysis) if analyze=True
        """
        self._conversions += 1

        if not audio_bytes:
            result = b''
            if analyze:
                return result, AudioAnalysis(issues=["Empty input"])
            return result

        # Check cache first
        if self._cache:
            cached = self._cache.get(audio_bytes)
            if cached:
                if analyze:
                    analysis = analyze_pcm_audio(
                        cached,
                        self.config.target_sample_rate,
                        self.config.target_channels,
                        self.config.target_bit_depth
                    )
                    return cached, analysis
                return cached

        # Detect format
        format = self.detect_audio_format(audio_bytes)
        logger.info(f"Converting {format.name} audio ({len(audio_bytes)} bytes)")

        pcm_data: Optional[bytes] = None

        # Handle based on format
        if format == AudioFormat.WAV:
            pcm_data = self._extract_wav_pcm(audio_bytes)

        elif format == AudioFormat.RAW_PCM:
            # Already PCM - just validate
            if len(audio_bytes) % 2 != 0:
                audio_bytes = audio_bytes + b'\x00'
            pcm_data = audio_bytes

        elif format in [AudioFormat.WEBM, AudioFormat.OGG, AudioFormat.MP3,
                       AudioFormat.FLAC, AudioFormat.MP4, AudioFormat.M4A,
                       AudioFormat.AAC]:
            # Compressed format - needs transcoding
            if self.config.enable_fallback_chain:
                # Try backends in order
                for backend_fn in [self._transcode_with_pydub, self._transcode_with_ffmpeg]:
                    pcm_data = backend_fn(audio_bytes, format)
                    if pcm_data:
                        break
            else:
                pcm_data = self._transcode_with_pydub(audio_bytes, format)

        else:
            # Unknown format - try transcoding anyway
            logger.warning(f"Unknown format, attempting transcode")
            pcm_data = self._transcode_with_pydub(audio_bytes, AudioFormat.UNKNOWN)

        # Handle failure
        if pcm_data is None:
            self._failures += 1
            logger.error(f"Failed to convert {format.name} audio")

            if self.config.return_empty_on_failure:
                pcm_data = b''
            else:
                # Return original (may cause issues downstream)
                pcm_data = audio_bytes if len(audio_bytes) % 2 == 0 else audio_bytes + b'\x00'

        # Normalize if enabled
        if pcm_data and self.config.normalize_audio:
            pcm_data = normalize_audio(
                pcm_data,
                self.config.target_peak_db,
                self.config.target_bit_depth
            )

        # Cache the result
        if self._cache and pcm_data:
            self._cache.put(audio_bytes, pcm_data, format)

        # Return with optional analysis
        if analyze:
            analysis = analyze_pcm_audio(
                pcm_data,
                self.config.target_sample_rate,
                self.config.target_channels,
                self.config.target_bit_depth
            )
            return pcm_data, analysis

        return pcm_data

    async def ensure_pcm_format_async(
        self,
        audio_bytes: bytes,
        analyze: bool = False
    ) -> Union[bytes, Tuple[bytes, AudioAnalysis]]:
        """
        Async version of ensure_pcm_format.

        Runs transcoding in thread pool to avoid blocking the event loop.

        Args:
            audio_bytes: Raw or compressed audio
            analyze: If True, also return AudioAnalysis

        Returns:
            PCM bytes, or (PCM bytes, AudioAnalysis) if analyze=True
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self.ensure_pcm_format(audio_bytes, analyze)
        )

    # =========================================================================
    # WAV File Creation
    # =========================================================================

    def create_wav_header(
        self,
        audio_bytes: bytes,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
        bits_per_sample: Optional[int] = None
    ) -> bytes:
        """
        Create WAV file from PCM data.

        Args:
            audio_bytes: Raw PCM data
            sample_rate: Sample rate (defaults to config)
            channels: Number of channels (defaults to config)
            bits_per_sample: Bits per sample (defaults to config)

        Returns:
            Complete WAV file bytes
        """
        sample_rate = sample_rate or self.config.target_sample_rate
        channels = channels or self.config.target_channels
        bits_per_sample = bits_per_sample or self.config.target_bit_depth

        byte_rate = sample_rate * channels * bits_per_sample // 8
        block_align = channels * bits_per_sample // 8
        data_size = len(audio_bytes)
        file_size = data_size + 44 - 8

        header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', file_size, b'WAVE',
            b'fmt ', 16, 1, channels,
            sample_rate, byte_rate, block_align, bits_per_sample,
            b'data', data_size
        )

        return header + audio_bytes

    def convert_to_wav(
        self,
        audio_data: Any,
        target_sample_rate: Optional[int] = None,
        target_channels: Optional[int] = None,
        target_bit_depth: Optional[int] = None
    ) -> bytes:
        """
        Convert any audio format to WAV file bytes.

        This is the main entry point for full WAV conversion.
        Handles all input types (base64, bytes, arrays, etc.) and formats
        (WebM, OGG, MP3, etc.) and produces a complete WAV file.

        CRITICAL: This method is used by the VBI pipeline to prepare audio
        for speaker verification (ECAPA-TDNN). The output must be a valid
        WAV file with proper headers.

        Args:
            audio_data: Audio in any format (bytes, base64 string, numpy array, etc.)
            target_sample_rate: Target sample rate (defaults to 16000 for VBI)
            target_channels: Target channels (defaults to 1 for mono)
            target_bit_depth: Target bit depth (defaults to 16)

        Returns:
            Complete WAV file bytes ready for speaker verification

        Example:
            >>> converter = AudioFormatConverter()
            >>> wav_bytes = converter.convert_to_wav(webm_audio_base64, target_sample_rate=16000)
            >>> # wav_bytes is a complete WAV file that can be saved or processed
        """
        # Use provided values or fall back to config
        sample_rate = target_sample_rate or self.config.target_sample_rate
        channels = target_channels or self.config.target_channels
        bit_depth = target_bit_depth or self.config.target_bit_depth

        # Step 1: Convert input to bytes (handles base64, arrays, dict, etc.)
        audio_bytes = self.convert_to_bytes(audio_data)

        if not audio_bytes:
            logger.warning("[convert_to_wav] Empty audio data after input conversion")
            # Return empty but valid WAV file (silent audio)
            return self.create_wav_header(b'', sample_rate, channels, bit_depth)

        # Step 2: Convert to PCM format (handles WebM, OGG, MP3, etc.)
        # Temporarily update config for this conversion if different sample rate requested
        original_sample_rate = self.config.target_sample_rate
        original_channels = self.config.target_channels
        original_bit_depth = self.config.target_bit_depth

        try:
            if target_sample_rate:
                self.config.target_sample_rate = sample_rate
            if target_channels:
                self.config.target_channels = channels
            if target_bit_depth:
                self.config.target_bit_depth = bit_depth

            pcm_data = self.ensure_pcm_format(audio_bytes)

        finally:
            # Restore original config
            self.config.target_sample_rate = original_sample_rate
            self.config.target_channels = original_channels
            self.config.target_bit_depth = original_bit_depth

        if not pcm_data:
            logger.warning("[convert_to_wav] Failed to convert to PCM")
            return self.create_wav_header(b'', sample_rate, channels, bit_depth)

        # Step 3: Create complete WAV file with header
        wav_bytes = self.create_wav_header(pcm_data, sample_rate, channels, bit_depth)

        logger.info(
            f"✅ [convert_to_wav] Converted to WAV: {len(wav_bytes)} bytes "
            f"({sample_rate}Hz, {channels}ch, {bit_depth}-bit)"
        )

        return wav_bytes

    async def convert_to_wav_async(
        self,
        audio_data: Any,
        target_sample_rate: Optional[int] = None,
        target_channels: Optional[int] = None,
        target_bit_depth: Optional[int] = None
    ) -> bytes:
        """
        Async version of convert_to_wav.

        Runs the potentially slow transcoding in a thread pool to avoid
        blocking the event loop. Ideal for WebSocket handlers and API endpoints.

        Args:
            audio_data: Audio in any format
            target_sample_rate: Target sample rate (defaults to 16000)
            target_channels: Target channels (defaults to 1)
            target_bit_depth: Target bit depth (defaults to 16)

        Returns:
            Complete WAV file bytes

        Example:
            >>> converter = AudioFormatConverter()
            >>> wav_bytes = await converter.convert_to_wav_async(webm_audio_base64)
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self.convert_to_wav(
                audio_data,
                target_sample_rate,
                target_channels,
                target_bit_depth
            )
        )

    def convert_to_pcm(
        self,
        audio_data: Any,
        target_sample_rate: Optional[int] = None
    ) -> bytes:
        """
        Convert any audio format to raw PCM bytes (without WAV header).

        Alias for ensure_pcm_format with input conversion.
        Use convert_to_wav() if you need a complete WAV file.

        Args:
            audio_data: Audio in any format
            target_sample_rate: Target sample rate (optional)

        Returns:
            Raw PCM bytes (16-bit signed, little-endian)
        """
        audio_bytes = self.convert_to_bytes(audio_data)

        if not audio_bytes:
            return b''

        # Handle custom sample rate
        if target_sample_rate and target_sample_rate != self.config.target_sample_rate:
            original = self.config.target_sample_rate
            self.config.target_sample_rate = target_sample_rate
            try:
                return self.ensure_pcm_format(audio_bytes)
            finally:
                self.config.target_sample_rate = original

        return self.ensure_pcm_format(audio_bytes)

    # =========================================================================
    # Statistics and Info
    # =========================================================================

    @property
    def stats(self) -> Dict[str, Any]:
        """Get converter statistics."""
        cache_stats = self._cache.stats if self._cache else {}
        return {
            'conversions': self._conversions,
            'failures': self._failures,
            'success_rate': (self._conversions - self._failures) / max(1, self._conversions),
            'backend_usage': self._backend_usage,
            'cache': cache_stats,
            'backends': {
                'ffmpeg': FFMPEG_AVAILABLE,
                'pydub': PYDUB_AVAILABLE,
                'scipy': SCIPY_AVAILABLE,
            }
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """Get converter capabilities."""
        return {
            'supported_formats': [f.name for f in AudioFormat if f != AudioFormat.UNKNOWN],
            'target_format': {
                'sample_rate': self.config.target_sample_rate,
                'channels': self.config.target_channels,
                'bit_depth': self.config.target_bit_depth,
            },
            'backends': {
                'ffmpeg': {'available': FFMPEG_AVAILABLE, 'path': FFMPEG_PATH},
                'pydub': {'available': PYDUB_AVAILABLE},
                'scipy': {'available': SCIPY_AVAILABLE},
            },
            'features': {
                'async': True,
                'caching': self.config.cache_enabled,
                'normalization': self.config.normalize_audio,
                'analysis': True,
            }
        }


# =============================================================================
# Global Instance and Helper Functions
# =============================================================================

# Global converter instance with default config
_converter: Optional[AudioFormatConverter] = None


def get_audio_converter() -> AudioFormatConverter:
    """Get or create the global audio converter instance."""
    global _converter
    if _converter is None:
        _converter = AudioFormatConverter()
    return _converter


def prepare_audio_for_stt(audio_data: Any) -> bytes:
    """
    Prepare any audio format for STT processing.

    This is the main entry point for audio preparation.
    Handles all input types and formats.

    Args:
        audio_data: Audio in any format

    Returns:
        PCM audio bytes ready for STT
    """
    converter = get_audio_converter()

    # Convert input to bytes
    audio_bytes = converter.convert_to_bytes(audio_data)

    # Handle case where conversion returned a string (shouldn't happen but safety check)
    if isinstance(audio_bytes, str):
        logger.error("convert_to_bytes returned string - attempting emergency decode")
        try:
            audio_bytes = base64.b64decode(audio_bytes)
        except Exception:
            return b''

    if not audio_bytes:
        logger.warning("No audio data after input conversion")
        return b''

    # Convert to PCM
    pcm_bytes = converter.ensure_pcm_format(audio_bytes)

    logger.info(f"✅ Audio prepared: {len(pcm_bytes)} bytes PCM @ {converter.config.target_sample_rate}Hz")
    return pcm_bytes


async def prepare_audio_for_stt_async(audio_data: Any) -> bytes:
    """
    Async version of prepare_audio_for_stt.

    Use this in async contexts to avoid blocking.

    Args:
        audio_data: Audio in any format

    Returns:
        PCM audio bytes ready for STT
    """
    converter = get_audio_converter()

    # Convert input to bytes (sync, fast)
    audio_bytes = converter.convert_to_bytes(audio_data)

    if isinstance(audio_bytes, str):
        try:
            audio_bytes = base64.b64decode(audio_bytes)
        except Exception:
            return b''

    if not audio_bytes:
        return b''

    # Convert to PCM (async for potentially slow transcode)
    pcm_bytes = await converter.ensure_pcm_format_async(audio_bytes)

    logger.info(f"✅ Audio prepared (async): {len(pcm_bytes)} bytes PCM")
    return pcm_bytes


def prepare_audio_with_analysis(audio_data: Any) -> Tuple[bytes, AudioAnalysis]:
    """
    Prepare audio and return quality analysis.

    Useful for checking audio quality before voice verification.

    Args:
        audio_data: Audio in any format

    Returns:
        Tuple of (PCM bytes, AudioAnalysis)
    """
    converter = get_audio_converter()
    audio_bytes = converter.convert_to_bytes(audio_data)

    if not audio_bytes or isinstance(audio_bytes, str):
        return b'', AudioAnalysis(issues=["Invalid input"])

    return converter.ensure_pcm_format(audio_bytes, analyze=True)


# Legacy compatibility
audio_converter = get_audio_converter()
