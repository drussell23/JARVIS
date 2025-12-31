"""
Unified Async Microphone Manager
================================

A robust, production-grade microphone management system for JARVIS voice operations.

Features:
- Async-first design with full asyncio integration
- Multi-backend support (sounddevice primary, PyAudio fallback)
- Intelligent permission handling with macOS TCC integration
- Automatic device detection and failover
- Real-time audio quality monitoring
- Adaptive noise floor calibration
- Exponential backoff with jitter for error recovery
- Device health monitoring and auto-recovery
- Thread-safe singleton pattern for global access
- Environment-driven configuration (no hardcoding)

Architecture:
- MicrophoneManager: Singleton managing global microphone state
- AudioBackend: Abstract interface for audio backends
- SoundDeviceBackend: Primary async-compatible backend
- PyAudioBackend: Fallback synchronous backend
- MicrophoneHealthMonitor: Continuous device health checking
- AudioQualityAnalyzer: Real-time audio quality metrics
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import random
import subprocess
import threading
import time
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

@dataclass
class MicrophoneConfig:
    """Environment-driven microphone configuration with no hardcoding."""

    # Audio settings from environment
    sample_rate: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_SAMPLE_RATE", "16000"))
    )
    channels: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_CHANNELS", "1"))
    )
    chunk_duration_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_CHUNK_MS", "30"))
    )
    dtype: str = field(
        default_factory=lambda: os.getenv("JARVIS_MIC_DTYPE", "float32")
    )

    # Device selection
    preferred_device: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_MIC_DEVICE")
    )
    device_index: Optional[int] = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_DEVICE_INDEX", "-1")) if os.getenv("JARVIS_MIC_DEVICE_INDEX") else None
    )

    # Backend preference
    preferred_backend: str = field(
        default_factory=lambda: os.getenv("JARVIS_MIC_BACKEND", "sounddevice")
    )
    enable_fallback: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MIC_ENABLE_FALLBACK", "true").lower() == "true"
    )

    # Error recovery
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_MAX_RETRIES", "5"))
    )
    initial_retry_delay_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_RETRY_DELAY_MS", "100"))
    )
    max_retry_delay_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIC_MAX_RETRY_DELAY_MS", "5000"))
    )

    # Health monitoring
    health_check_interval_s: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIC_HEALTH_INTERVAL_S", "5.0"))
    )
    auto_recovery_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MIC_AUTO_RECOVERY", "true").lower() == "true"
    )

    # Audio quality
    noise_calibration_duration_s: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIC_NOISE_CALIBRATION_S", "1.0"))
    )
    min_snr_db: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIC_MIN_SNR_DB", "10.0"))
    )

    # VAD (Voice Activity Detection)
    vad_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MIC_VAD_ENABLED", "true").lower() == "true"
    )
    vad_energy_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIC_VAD_THRESHOLD", "0.02"))
    )
    vad_silence_duration_s: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIC_VAD_SILENCE_S", "1.5"))
    )

    @property
    def chunk_size(self) -> int:
        """Calculate chunk size in samples."""
        return int(self.sample_rate * self.chunk_duration_ms / 1000)

    @property
    def bytes_per_sample(self) -> int:
        """Calculate bytes per sample based on dtype."""
        dtype_sizes = {"float32": 4, "float64": 8, "int16": 2, "int32": 4}
        return dtype_sizes.get(self.dtype, 4)


# =============================================================================
# Enums and Status Types
# =============================================================================

class MicrophoneStatus(Enum):
    """Microphone operational status."""
    READY = auto()
    INITIALIZING = auto()
    CAPTURING = auto()
    PAUSED = auto()
    ERROR = auto()
    PERMISSION_DENIED = auto()
    DEVICE_NOT_FOUND = auto()
    DEVICE_BUSY = auto()
    RECOVERING = auto()
    SHUTDOWN = auto()


class AudioBackendType(Enum):
    """Available audio backend types."""
    SOUNDDEVICE = "sounddevice"
    PYAUDIO = "pyaudio"
    NONE = "none"


class ErrorCategory(Enum):
    """Categories of microphone errors for intelligent handling."""
    PERMISSION = auto()
    DEVICE_NOT_FOUND = auto()
    DEVICE_BUSY = auto()
    OVERFLOW = auto()
    UNDERFLOW = auto()
    FORMAT_ERROR = auto()
    TIMEOUT = auto()
    BACKEND_CRASH = auto()
    UNKNOWN = auto()


@dataclass
class MicrophoneError:
    """Structured microphone error with recovery hints."""
    category: ErrorCategory
    message: str
    original_exception: Optional[Exception] = None
    timestamp: datetime = field(default_factory=datetime.now)
    recoverable: bool = True
    suggested_action: str = ""
    retry_delay_ms: int = 100

    def __post_init__(self):
        """Set recovery hints based on category."""
        category_hints = {
            ErrorCategory.PERMISSION: (
                True,
                "Grant microphone permission in System Preferences > Security & Privacy > Microphone",
                1000,
            ),
            ErrorCategory.DEVICE_NOT_FOUND: (
                True,
                "Connect a microphone or select a different device",
                500,
            ),
            ErrorCategory.DEVICE_BUSY: (
                True,
                "Close other applications using the microphone (Zoom, Teams, etc.)",
                2000,
            ),
            ErrorCategory.OVERFLOW: (
                True,
                "Reduce audio processing load or increase buffer size",
                100,
            ),
            ErrorCategory.UNDERFLOW: (
                True,
                "Check audio driver stability",
                100,
            ),
            ErrorCategory.FORMAT_ERROR: (
                True,
                "Check audio format compatibility",
                500,
            ),
            ErrorCategory.TIMEOUT: (
                True,
                "Retry operation or restart audio service",
                1000,
            ),
            ErrorCategory.BACKEND_CRASH: (
                True,
                "Switching to fallback audio backend",
                500,
            ),
            ErrorCategory.UNKNOWN: (
                False,
                "Contact support if issue persists",
                2000,
            ),
        }

        if not self.suggested_action:
            hints = category_hints.get(self.category, (False, "Unknown error", 1000))
            self.recoverable = hints[0]
            self.suggested_action = hints[1]
            self.retry_delay_ms = hints[2]


@dataclass
class AudioDeviceInfo:
    """Information about an audio input device."""
    index: int
    name: str
    channels: int
    sample_rate: float
    is_default: bool = False
    is_available: bool = True
    backend: AudioBackendType = AudioBackendType.SOUNDDEVICE

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "index": self.index,
            "name": self.name,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "is_default": self.is_default,
            "is_available": self.is_available,
            "backend": self.backend.value,
        }


@dataclass
class AudioQualityMetrics:
    """Real-time audio quality metrics."""
    rms_energy: float = 0.0
    peak_amplitude: float = 0.0
    snr_db: float = 0.0
    noise_floor: float = 0.0
    clipping_detected: bool = False
    silence_detected: bool = False
    sample_count: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "rms_energy": round(self.rms_energy, 6),
            "peak_amplitude": round(self.peak_amplitude, 6),
            "snr_db": round(self.snr_db, 2),
            "noise_floor": round(self.noise_floor, 6),
            "clipping_detected": self.clipping_detected,
            "silence_detected": self.silence_detected,
            "sample_count": self.sample_count,
            "timestamp": self.timestamp.isoformat(),
        }


# =============================================================================
# Audio Quality Analyzer
# =============================================================================

class AudioQualityAnalyzer:
    """Real-time audio quality analysis."""

    def __init__(self, config: MicrophoneConfig):
        self.config = config
        self.noise_floor: float = 0.0
        self.calibrated: bool = False
        self._energy_history: List[float] = []
        self._history_size: int = 100

    def analyze(self, audio_data: np.ndarray) -> AudioQualityMetrics:
        """Analyze audio chunk and return quality metrics."""
        # Ensure float32 for calculations
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)
            if audio_data.dtype == np.int16:
                audio_data = audio_data / 32768.0

        # Calculate metrics
        rms = float(np.sqrt(np.mean(audio_data ** 2)))
        peak = float(np.max(np.abs(audio_data)))

        # Update energy history
        self._energy_history.append(rms)
        if len(self._energy_history) > self._history_size:
            self._energy_history.pop(0)

        # Calculate SNR if calibrated
        snr_db = 0.0
        if self.calibrated and self.noise_floor > 0:
            signal_power = rms ** 2
            noise_power = self.noise_floor ** 2
            if signal_power > noise_power:
                snr_db = 10 * np.log10(signal_power / noise_power)

        return AudioQualityMetrics(
            rms_energy=rms,
            peak_amplitude=peak,
            snr_db=snr_db,
            noise_floor=self.noise_floor,
            clipping_detected=peak > 0.99,
            silence_detected=rms < self.config.vad_energy_threshold,
            sample_count=len(audio_data),
        )

    def calibrate_noise_floor(self, audio_samples: List[np.ndarray]) -> float:
        """Calibrate noise floor from quiet audio samples."""
        if not audio_samples:
            return 0.0

        # Combine samples and calculate noise floor
        combined = np.concatenate(audio_samples)
        if combined.dtype != np.float32:
            combined = combined.astype(np.float32)
            if combined.dtype == np.int16:
                combined = combined / 32768.0

        self.noise_floor = float(np.sqrt(np.mean(combined ** 2)))
        self.calibrated = True
        logger.info(f"Noise floor calibrated: {self.noise_floor:.6f}")
        return self.noise_floor

    def get_adaptive_threshold(self) -> float:
        """Get adaptive energy threshold based on recent history."""
        if len(self._energy_history) < 10:
            return self.config.vad_energy_threshold

        median_energy = float(np.median(self._energy_history))
        return max(self.config.vad_energy_threshold, median_energy * 2.0)


# =============================================================================
# Audio Backend Abstract Interface
# =============================================================================

class AudioBackend(ABC):
    """Abstract interface for audio backends."""

    def __init__(self, config: MicrophoneConfig):
        self.config = config
        self._initialized: bool = False
        self._stream: Any = None
        self._device_index: Optional[int] = None
        self._lock = threading.Lock()

    @property
    @abstractmethod
    def backend_type(self) -> AudioBackendType:
        """Return the backend type."""
        pass

    @property
    def is_initialized(self) -> bool:
        """Check if backend is initialized."""
        return self._initialized

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize the audio backend."""
        pass

    @abstractmethod
    async def open_stream(self, device_index: Optional[int] = None) -> bool:
        """Open an audio input stream."""
        pass

    @abstractmethod
    async def close_stream(self) -> bool:
        """Close the audio input stream."""
        pass

    @abstractmethod
    async def read_chunk(self) -> Optional[np.ndarray]:
        """Read a chunk of audio data."""
        pass

    @abstractmethod
    async def list_devices(self) -> List[AudioDeviceInfo]:
        """List available audio input devices."""
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up backend resources."""
        pass

    @abstractmethod
    def is_stream_active(self) -> bool:
        """Check if the stream is active."""
        pass


# =============================================================================
# SoundDevice Backend (Primary - Async Compatible)
# =============================================================================

class SoundDeviceBackend(AudioBackend):
    """Primary audio backend using sounddevice (async compatible)."""

    def __init__(self, config: MicrophoneConfig):
        super().__init__(config)
        self._sd: Any = None
        # Use thread-safe queue to avoid event loop issues in sync contexts
        self._audio_queue: queue.Queue = queue.Queue(maxsize=100)
        self._overflow_count: int = 0
        self._stream_callback_active: bool = False

    @property
    def backend_type(self) -> AudioBackendType:
        return AudioBackendType.SOUNDDEVICE

    async def initialize(self) -> bool:
        """Initialize sounddevice backend."""
        if self._initialized:
            return True

        try:
            import sounddevice as sd
            self._sd = sd

            # Test basic functionality
            devices = self._sd.query_devices()
            logger.info(f"SoundDevice initialized with {len(devices)} devices")

            self._initialized = True
            return True

        except ImportError as e:
            logger.error(f"sounddevice not installed: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize sounddevice: {e}")
            return False

    async def open_stream(self, device_index: Optional[int] = None) -> bool:
        """Open sounddevice input stream."""
        if not self._initialized:
            if not await self.initialize():
                return False

        try:
            with self._lock:
                if self._stream is not None:
                    await self.close_stream()

                self._device_index = device_index
                self._overflow_count = 0

                # Clear any stale data from queue
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break

                # Create callback-based stream for async operation
                def audio_callback(indata, frames, time_info, status):
                    if status:
                        if status.input_overflow:
                            self._overflow_count += 1
                            if self._overflow_count % 100 == 0:
                                logger.warning(f"Audio overflow #{self._overflow_count}")

                    try:
                        # Copy audio data to thread-safe queue
                        audio_copy = indata.copy().flatten()
                        try:
                            self._audio_queue.put_nowait(audio_copy)
                        except queue.Full:
                            pass  # Drop frame if queue is full
                    except Exception as e:
                        logger.error(f"Audio callback error: {e}")

                self._stream = self._sd.InputStream(
                    device=device_index,
                    samplerate=self.config.sample_rate,
                    channels=self.config.channels,
                    dtype=self.config.dtype,
                    blocksize=self.config.chunk_size,
                    callback=audio_callback,
                )
                self._stream.start()
                self._stream_callback_active = True

                logger.info(f"SoundDevice stream opened (device: {device_index})")
                return True

        except Exception as e:
            logger.error(f"Failed to open sounddevice stream: {e}")
            self._stream = None
            return False

    async def close_stream(self) -> bool:
        """Close sounddevice stream."""
        try:
            with self._lock:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
                    self._stream = None
                    self._stream_callback_active = False

                    # Clear audio queue
                    while not self._audio_queue.empty():
                        try:
                            self._audio_queue.get_nowait()
                        except queue.Empty:
                            break

                    logger.info("SoundDevice stream closed")
                return True
        except Exception as e:
            logger.error(f"Error closing sounddevice stream: {e}")
            return False

    async def read_chunk(self) -> Optional[np.ndarray]:
        """Read audio chunk from thread-safe queue (non-blocking async)."""
        if not self._stream_callback_active:
            return None

        try:
            # First try non-blocking get
            try:
                audio_data = self._audio_queue.get_nowait()
                return audio_data
            except queue.Empty:
                pass

            # If queue is empty, wait a bit and try again
            # Use a short sleep to yield to event loop
            await asyncio.sleep(0.01)

            try:
                audio_data = self._audio_queue.get_nowait()
                return audio_data
            except queue.Empty:
                return None

        except Exception as e:
            logger.error(f"Error reading audio chunk: {e}")
            return None

    async def list_devices(self) -> List[AudioDeviceInfo]:
        """List available audio input devices."""
        if not self._initialized:
            if not await self.initialize():
                return []

        try:
            devices = []
            all_devices = self._sd.query_devices()
            default_input = self._sd.default.device[0]

            for i, dev in enumerate(all_devices):
                if dev['max_input_channels'] > 0:
                    devices.append(AudioDeviceInfo(
                        index=i,
                        name=dev['name'],
                        channels=dev['max_input_channels'],
                        sample_rate=dev['default_samplerate'],
                        is_default=(i == default_input),
                        is_available=True,
                        backend=self.backend_type,
                    ))

            return devices

        except Exception as e:
            logger.error(f"Error listing devices: {e}")
            return []

    async def cleanup(self) -> None:
        """Clean up sounddevice resources."""
        await self.close_stream()
        self._initialized = False
        logger.info("SoundDevice backend cleaned up")

    def is_stream_active(self) -> bool:
        """Check if stream is active."""
        return self._stream is not None and self._stream_callback_active


# =============================================================================
# PyAudio Backend (Fallback - Synchronous)
# =============================================================================

class PyAudioBackend(AudioBackend):
    """Fallback audio backend using PyAudio."""

    def __init__(self, config: MicrophoneConfig):
        super().__init__(config)
        self._pyaudio: Any = None
        self._audio_instance: Any = None
        self._read_queue: queue.Queue = queue.Queue(maxsize=100)
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    @property
    def backend_type(self) -> AudioBackendType:
        return AudioBackendType.PYAUDIO

    async def initialize(self) -> bool:
        """Initialize PyAudio backend."""
        if self._initialized:
            return True

        try:
            import pyaudio
            self._pyaudio = pyaudio
            self._audio_instance = pyaudio.PyAudio()

            # Test basic functionality
            device_count = self._audio_instance.get_device_count()
            logger.info(f"PyAudio initialized with {device_count} devices")

            self._initialized = True
            return True

        except ImportError as e:
            logger.error(f"PyAudio not installed: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize PyAudio: {e}")
            return False

    def _get_pyaudio_format(self) -> int:
        """Get PyAudio format from dtype."""
        formats = {
            "float32": self._pyaudio.paFloat32,
            "int16": self._pyaudio.paInt16,
            "int32": self._pyaudio.paInt32,
        }
        return formats.get(self.config.dtype, self._pyaudio.paInt16)

    async def open_stream(self, device_index: Optional[int] = None) -> bool:
        """Open PyAudio input stream."""
        if not self._initialized:
            if not await self.initialize():
                return False

        try:
            with self._lock:
                if self._stream is not None:
                    await self.close_stream()

                self._device_index = device_index
                self._stop_event.clear()

                # Open PyAudio stream
                self._stream = self._audio_instance.open(
                    format=self._get_pyaudio_format(),
                    channels=self.config.channels,
                    rate=self.config.sample_rate,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=self.config.chunk_size,
                )

                # Start capture thread
                self._capture_thread = threading.Thread(
                    target=self._capture_loop,
                    daemon=True,
                    name="PyAudioCapture"
                )
                self._capture_thread.start()

                logger.info(f"PyAudio stream opened (device: {device_index})")
                return True

        except Exception as e:
            logger.error(f"Failed to open PyAudio stream: {e}")
            self._stream = None
            return False

    def _capture_loop(self):
        """Background thread for capturing audio."""
        while not self._stop_event.is_set():
            try:
                if self._stream is None:
                    break

                # Read audio data
                data = self._stream.read(
                    self.config.chunk_size,
                    exception_on_overflow=False
                )

                # Convert to numpy array
                if self.config.dtype == "int16":
                    audio_array = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                else:
                    audio_array = np.frombuffer(data, dtype=np.float32)

                # Put in queue
                try:
                    self._read_queue.put_nowait(audio_array)
                except queue.Full:
                    pass  # Drop frame if queue is full

            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"PyAudio capture error: {e}")
                break

    async def close_stream(self) -> bool:
        """Close PyAudio stream."""
        try:
            with self._lock:
                # Stop capture thread
                self._stop_event.set()
                if self._capture_thread is not None:
                    self._capture_thread.join(timeout=1.0)
                    self._capture_thread = None

                # Close stream
                if self._stream is not None:
                    self._stream.stop_stream()
                    self._stream.close()
                    self._stream = None

                # Clear queue
                while not self._read_queue.empty():
                    try:
                        self._read_queue.get_nowait()
                    except queue.Empty:
                        break

                logger.info("PyAudio stream closed")
                return True

        except Exception as e:
            logger.error(f"Error closing PyAudio stream: {e}")
            return False

    async def read_chunk(self) -> Optional[np.ndarray]:
        """Read audio chunk from queue."""
        try:
            audio_data = self._read_queue.get(timeout=0.5)
            return audio_data
        except queue.Empty:
            return None
        except Exception as e:
            logger.error(f"Error reading audio chunk: {e}")
            return None

    async def list_devices(self) -> List[AudioDeviceInfo]:
        """List available audio input devices."""
        if not self._initialized:
            if not await self.initialize():
                return []

        try:
            devices = []
            default_input = self._audio_instance.get_default_input_device_info()['index']

            for i in range(self._audio_instance.get_device_count()):
                info = self._audio_instance.get_device_info_by_index(i)
                if info['maxInputChannels'] > 0:
                    devices.append(AudioDeviceInfo(
                        index=i,
                        name=info['name'],
                        channels=info['maxInputChannels'],
                        sample_rate=info['defaultSampleRate'],
                        is_default=(i == default_input),
                        is_available=True,
                        backend=self.backend_type,
                    ))

            return devices

        except Exception as e:
            logger.error(f"Error listing devices: {e}")
            return []

    async def cleanup(self) -> None:
        """Clean up PyAudio resources."""
        await self.close_stream()

        if self._audio_instance is not None:
            self._audio_instance.terminate()
            self._audio_instance = None

        self._initialized = False
        logger.info("PyAudio backend cleaned up")

    def is_stream_active(self) -> bool:
        """Check if stream is active."""
        return (
            self._stream is not None and
            self._capture_thread is not None and
            self._capture_thread.is_alive()
        )


# =============================================================================
# Permission Handler
# =============================================================================

class MicrophonePermissionHandler:
    """Handles microphone permission checking and requests on macOS."""

    def __init__(self):
        self._permission_granted: Optional[bool] = None
        self._last_check: Optional[datetime] = None
        self._check_interval = timedelta(seconds=30)

    async def check_permission(self) -> bool:
        """Check if microphone permission is granted."""
        # Use cached result if recent
        if (
            self._permission_granted is not None and
            self._last_check is not None and
            datetime.now() - self._last_check < self._check_interval
        ):
            return self._permission_granted

        # Try quick test first - actually try to open the microphone
        try:
            import sounddevice as sd

            def quick_test():
                try:
                    # Try to query devices - this doesn't require permission
                    devices = sd.query_devices()
                    # Check if any input device exists
                    for dev in devices:
                        if isinstance(dev, dict) and dev.get('max_input_channels', 0) > 0:
                            return True
                    return False
                except Exception:
                    return False

            loop = asyncio.get_running_loop()
            has_devices = await loop.run_in_executor(None, quick_test)

            if has_devices:
                # Devices available, assume permission granted
                # (actual access will be tested when we open the stream)
                self._permission_granted = True
                self._last_check = datetime.now()
                return True

        except Exception as e:
            logger.debug(f"Quick permission test failed: {e}")

        # Fallback to AppleScript check with short timeout
        try:
            script = """
            use framework "AVFoundation"
            set authStatus to current application's AVCaptureDevice's authorizationStatusForMediaType:"soun"
            if authStatus = 3 then
                return "granted"
            else if authStatus = 2 then
                return "denied"
            else if authStatus = 0 then
                return "not_determined"
            else
                return "restricted"
            end if
            """

            result = await asyncio.create_subprocess_exec(
                "osascript", "-l", "AppleScript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=2.0)
            output = stdout.decode().strip().lower()

            self._permission_granted = "granted" in output or "not_determined" in output
            self._last_check = datetime.now()

            if not self._permission_granted:
                logger.warning(f"Microphone permission status: {output}")

            return self._permission_granted

        except asyncio.TimeoutError:
            logger.debug("Permission check timed out, assuming granted")
            self._permission_granted = True
            self._last_check = datetime.now()
            return True
        except Exception as e:
            logger.debug(f"Permission check failed: {e}, assuming granted")
            self._permission_granted = True
            self._last_check = datetime.now()
            return True

    async def request_permission(self) -> bool:
        """Request microphone permission (triggers system dialog)."""
        try:
            import sounddevice as sd

            def record_test():
                try:
                    # Brief test recording to trigger permission dialog
                    sd.rec(frames=1000, samplerate=16000, channels=1, dtype='float32')
                    sd.wait()
                    return True
                except Exception:
                    return False

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, record_test)

            if result:
                self._permission_granted = True
                self._last_check = datetime.now()

            return result

        except Exception as e:
            logger.error(f"Failed to request permission: {e}")
            return False

    async def open_permission_settings(self) -> bool:
        """Open System Preferences to microphone settings."""
        try:
            url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
            result = await asyncio.create_subprocess_exec(
                "open", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Failed to open settings: {e}")
            return False


# =============================================================================
# Error Classifier
# =============================================================================

class MicrophoneErrorClassifier:
    """Classifies exceptions into error categories."""

    @staticmethod
    def classify(exception: Exception) -> MicrophoneError:
        """Classify an exception into a MicrophoneError."""
        error_msg = str(exception).lower()

        # Permission errors
        if any(term in error_msg for term in [
            "permission", "not authorized", "access denied", "tcc"
        ]):
            return MicrophoneError(
                category=ErrorCategory.PERMISSION,
                message=str(exception),
                original_exception=exception,
            )

        # Device not found
        if any(term in error_msg for term in [
            "no device", "device not found", "invalid device", "no such device"
        ]):
            return MicrophoneError(
                category=ErrorCategory.DEVICE_NOT_FOUND,
                message=str(exception),
                original_exception=exception,
            )

        # Device busy
        if any(term in error_msg for term in [
            "device busy", "resource busy", "in use", "already open"
        ]):
            return MicrophoneError(
                category=ErrorCategory.DEVICE_BUSY,
                message=str(exception),
                original_exception=exception,
            )

        # Overflow/underflow
        if "overflow" in error_msg:
            return MicrophoneError(
                category=ErrorCategory.OVERFLOW,
                message=str(exception),
                original_exception=exception,
            )

        if "underflow" in error_msg:
            return MicrophoneError(
                category=ErrorCategory.UNDERFLOW,
                message=str(exception),
                original_exception=exception,
            )

        # Timeout
        if "timeout" in error_msg:
            return MicrophoneError(
                category=ErrorCategory.TIMEOUT,
                message=str(exception),
                original_exception=exception,
            )

        # Backend crash
        if any(term in error_msg for term in [
            "segfault", "core dumped", "aborted", "fatal"
        ]):
            return MicrophoneError(
                category=ErrorCategory.BACKEND_CRASH,
                message=str(exception),
                original_exception=exception,
            )

        # Unknown
        return MicrophoneError(
            category=ErrorCategory.UNKNOWN,
            message=str(exception),
            original_exception=exception,
        )


# =============================================================================
# Unified Microphone Manager (Singleton)
# =============================================================================

class MicrophoneManager:
    """
    Unified singleton microphone manager for JARVIS.

    Provides:
    - Centralized microphone access control
    - Automatic backend selection with fallback
    - Robust error recovery with exponential backoff
    - Device health monitoring
    - Permission handling
    - Real-time audio quality metrics
    """

    _instance: Optional["MicrophoneManager"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, config: Optional[MicrophoneConfig] = None):
        """Singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, config: Optional[MicrophoneConfig] = None):
        """Initialize the microphone manager."""
        if self._initialized:
            return

        self.config = config or MicrophoneConfig()

        # State
        self._status: MicrophoneStatus = MicrophoneStatus.INITIALIZING
        self._active_backend: Optional[AudioBackend] = None
        self._backends: Dict[AudioBackendType, AudioBackend] = {}

        # Components
        self._permission_handler = MicrophonePermissionHandler()
        self._quality_analyzer = AudioQualityAnalyzer(self.config)
        self._error_classifier = MicrophoneErrorClassifier()

        # Error tracking
        self._consecutive_errors: int = 0
        self._last_error: Optional[MicrophoneError] = None
        self._error_history: List[MicrophoneError] = []

        # Callbacks
        self._on_audio_callbacks: List[Callable[[np.ndarray, AudioQualityMetrics], None]] = []
        self._on_status_change_callbacks: List[Callable[[MicrophoneStatus], None]] = []
        self._on_error_callbacks: List[Callable[[MicrophoneError], None]] = []

        # Health monitoring
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._capture_task: Optional[asyncio.Task] = None
        self._shutdown_event: asyncio.Event = asyncio.Event()

        # Statistics
        self._stats = {
            "total_chunks_captured": 0,
            "total_errors": 0,
            "backend_switches": 0,
            "recovery_attempts": 0,
            "successful_recoveries": 0,
            "start_time": None,
        }

        self._initialized = True
        logger.info("MicrophoneManager initialized (singleton)")

    @classmethod
    def get_instance(cls, config: Optional[MicrophoneConfig] = None) -> "MicrophoneManager":
        """Get the singleton instance."""
        return cls(config)

    @property
    def status(self) -> MicrophoneStatus:
        """Get current microphone status."""
        return self._status

    @property
    def is_capturing(self) -> bool:
        """Check if currently capturing audio."""
        return self._status == MicrophoneStatus.CAPTURING

    @property
    def active_backend_type(self) -> AudioBackendType:
        """Get the active backend type."""
        if self._active_backend:
            return self._active_backend.backend_type
        return AudioBackendType.NONE

    def _set_status(self, status: MicrophoneStatus) -> None:
        """Set status and notify callbacks."""
        if status != self._status:
            old_status = self._status
            self._status = status
            logger.info(f"Microphone status: {old_status.name} -> {status.name}")

            for callback in self._on_status_change_callbacks:
                try:
                    callback(status)
                except Exception as e:
                    logger.error(f"Status callback error: {e}")

    def _handle_error(self, error: MicrophoneError) -> None:
        """Handle a microphone error."""
        self._last_error = error
        self._error_history.append(error)
        self._consecutive_errors += 1
        self._stats["total_errors"] += 1

        # Keep error history bounded
        if len(self._error_history) > 100:
            self._error_history = self._error_history[-50:]

        logger.error(
            f"Microphone error ({error.category.name}): {error.message} "
            f"[consecutive: {self._consecutive_errors}]"
        )

        for callback in self._on_error_callbacks:
            try:
                callback(error)
            except Exception as e:
                logger.error(f"Error callback error: {e}")

    async def _calculate_retry_delay(self) -> float:
        """Calculate retry delay with exponential backoff and jitter."""
        base_delay = self.config.initial_retry_delay_ms
        max_delay = self.config.max_retry_delay_ms

        # Exponential backoff
        delay = base_delay * (2 ** min(self._consecutive_errors, 10))
        delay = min(delay, max_delay)

        # Add jitter (10-30% randomization)
        jitter = delay * random.uniform(0.1, 0.3)
        delay += jitter

        return delay / 1000.0  # Convert to seconds

    async def initialize(self) -> bool:
        """Initialize the microphone manager and backends."""
        self._set_status(MicrophoneStatus.INITIALIZING)

        # Check permission first
        if not await self._permission_handler.check_permission():
            self._set_status(MicrophoneStatus.PERMISSION_DENIED)
            logger.error("Microphone permission denied")
            return False

        # Initialize backends
        await self._initialize_backends()

        if not self._active_backend:
            self._set_status(MicrophoneStatus.ERROR)
            return False

        self._set_status(MicrophoneStatus.READY)
        self._stats["start_time"] = datetime.now()
        return True

    async def _initialize_backends(self) -> None:
        """Initialize audio backends with fallback."""
        # Try preferred backend first
        if self.config.preferred_backend == "sounddevice":
            backends_to_try = [AudioBackendType.SOUNDDEVICE, AudioBackendType.PYAUDIO]
        else:
            backends_to_try = [AudioBackendType.PYAUDIO, AudioBackendType.SOUNDDEVICE]

        if not self.config.enable_fallback:
            backends_to_try = backends_to_try[:1]

        for backend_type in backends_to_try:
            try:
                backend = self._create_backend(backend_type)
                if backend and await backend.initialize():
                    self._backends[backend_type] = backend
                    if self._active_backend is None:
                        self._active_backend = backend
                        logger.info(f"Active backend: {backend_type.value}")
            except Exception as e:
                logger.warning(f"Failed to initialize {backend_type.value}: {e}")

    def _create_backend(self, backend_type: AudioBackendType) -> Optional[AudioBackend]:
        """Create a backend instance."""
        if backend_type == AudioBackendType.SOUNDDEVICE:
            return SoundDeviceBackend(self.config)
        elif backend_type == AudioBackendType.PYAUDIO:
            return PyAudioBackend(self.config)
        return None

    async def _switch_backend(self) -> bool:
        """Switch to a fallback backend."""
        if not self.config.enable_fallback:
            return False

        current_type = self._active_backend.backend_type if self._active_backend else None

        for backend_type, backend in self._backends.items():
            if backend_type != current_type and backend.is_initialized:
                logger.info(f"Switching backend: {current_type} -> {backend_type}")
                self._active_backend = backend
                self._stats["backend_switches"] += 1
                return True

        return False

    async def list_devices(self) -> List[AudioDeviceInfo]:
        """List all available audio input devices."""
        devices = []

        for backend in self._backends.values():
            if backend.is_initialized:
                backend_devices = await backend.list_devices()
                devices.extend(backend_devices)

        # Remove duplicates by device name
        seen = set()
        unique_devices = []
        for device in devices:
            if device.name not in seen:
                seen.add(device.name)
                unique_devices.append(device)

        return unique_devices

    async def start_capture(
        self,
        device_index: Optional[int] = None,
        on_audio: Optional[Callable[[np.ndarray, AudioQualityMetrics], None]] = None,
    ) -> bool:
        """
        Start capturing audio.

        Args:
            device_index: Optional device index to use
            on_audio: Callback for audio chunks with quality metrics
        """
        if self._status == MicrophoneStatus.CAPTURING:
            logger.warning("Already capturing")
            return True

        if not self._active_backend:
            if not await self.initialize():
                return False

        # Add callback if provided
        if on_audio:
            self._on_audio_callbacks.append(on_audio)

        # Use configured device if not specified
        if device_index is None:
            device_index = self.config.device_index

        # Open stream
        try:
            if not await self._active_backend.open_stream(device_index):
                self._set_status(MicrophoneStatus.ERROR)
                return False

            self._set_status(MicrophoneStatus.CAPTURING)
            self._consecutive_errors = 0

            # Start capture task
            self._shutdown_event.clear()
            self._capture_task = asyncio.create_task(
                self._capture_loop(),
                name="MicrophoneCapture"
            )

            # Start health monitor
            if self.config.auto_recovery_enabled:
                self._health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(),
                    name="MicrophoneHealthMonitor"
                )

            logger.info("Audio capture started")
            return True

        except Exception as e:
            error = self._error_classifier.classify(e)
            self._handle_error(error)
            self._set_status(MicrophoneStatus.ERROR)
            return False

    async def _capture_loop(self) -> None:
        """Main audio capture loop."""
        while not self._shutdown_event.is_set():
            try:
                if self._active_backend is None:
                    break

                # Read audio chunk
                audio_chunk = await self._active_backend.read_chunk()

                if audio_chunk is not None:
                    self._consecutive_errors = 0
                    self._stats["total_chunks_captured"] += 1

                    # Analyze quality
                    quality = self._quality_analyzer.analyze(audio_chunk)

                    # Notify callbacks
                    for callback in self._on_audio_callbacks:
                        try:
                            callback(audio_chunk, quality)
                        except Exception as e:
                            logger.error(f"Audio callback error: {e}")
                else:
                    # No audio - might be timeout or error
                    await asyncio.sleep(0.01)

            except asyncio.CancelledError:
                break
            except Exception as e:
                error = self._error_classifier.classify(e)
                self._handle_error(error)

                if error.recoverable and self._consecutive_errors < self.config.max_retries:
                    delay = await self._calculate_retry_delay()
                    logger.info(f"Retrying in {delay:.2f}s...")
                    await asyncio.sleep(delay)

                    # Try to recover
                    if await self._attempt_recovery():
                        continue
                else:
                    self._set_status(MicrophoneStatus.ERROR)
                    break

    async def _attempt_recovery(self) -> bool:
        """Attempt to recover from an error."""
        self._set_status(MicrophoneStatus.RECOVERING)
        self._stats["recovery_attempts"] += 1

        # Try 1: Restart current stream
        try:
            await self._active_backend.close_stream()
            await asyncio.sleep(0.5)
            if await self._active_backend.open_stream(self._active_backend._device_index):
                self._set_status(MicrophoneStatus.CAPTURING)
                self._stats["successful_recoveries"] += 1
                logger.info("Recovery successful: stream restarted")
                return True
        except Exception as e:
            logger.warning(f"Stream restart failed: {e}")

        # Try 2: Switch backend
        if await self._switch_backend():
            try:
                if await self._active_backend.open_stream():
                    self._set_status(MicrophoneStatus.CAPTURING)
                    self._stats["successful_recoveries"] += 1
                    logger.info("Recovery successful: backend switched")
                    return True
            except Exception as e:
                logger.warning(f"Backend switch failed: {e}")

        # Try 3: Find different device
        devices = await self.list_devices()
        for device in devices:
            if device.is_available and device.index != self._active_backend._device_index:
                try:
                    if await self._active_backend.open_stream(device.index):
                        self._set_status(MicrophoneStatus.CAPTURING)
                        self._stats["successful_recoveries"] += 1
                        logger.info(f"Recovery successful: switched to {device.name}")
                        return True
                except Exception as e:
                    logger.warning(f"Device switch to {device.name} failed: {e}")

        return False

    async def _health_monitor_loop(self) -> None:
        """Monitor microphone health and trigger recovery if needed."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.health_check_interval_s)

                if self._status != MicrophoneStatus.CAPTURING:
                    continue

                # Check if stream is still active
                if self._active_backend and not self._active_backend.is_stream_active():
                    logger.warning("Stream appears inactive, triggering recovery")
                    await self._attempt_recovery()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health monitor error: {e}")

    async def stop_capture(self) -> bool:
        """Stop capturing audio."""
        self._shutdown_event.set()

        # Cancel tasks
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await asyncio.wait_for(self._capture_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if self._health_monitor_task and not self._health_monitor_task.done():
            self._health_monitor_task.cancel()
            try:
                await asyncio.wait_for(self._health_monitor_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Close stream
        if self._active_backend:
            await self._active_backend.close_stream()

        self._set_status(MicrophoneStatus.READY)
        logger.info("Audio capture stopped")
        return True

    async def calibrate_noise(self, duration: Optional[float] = None) -> float:
        """Calibrate noise floor."""
        duration = duration or self.config.noise_calibration_duration_s

        samples = []
        start_time = time.time()

        while time.time() - start_time < duration:
            if self._active_backend:
                chunk = await self._active_backend.read_chunk()
                if chunk is not None:
                    samples.append(chunk)
            await asyncio.sleep(0.01)

        return self._quality_analyzer.calibrate_noise_floor(samples)

    async def capture_audio(
        self,
        duration: float,
        include_quality: bool = False,
    ) -> Union[np.ndarray, Tuple[np.ndarray, List[AudioQualityMetrics]]]:
        """
        Capture audio for a specified duration.

        Args:
            duration: Duration in seconds
            include_quality: Whether to return quality metrics

        Returns:
            Audio array, or tuple of (audio, quality_metrics)
        """
        chunks = []
        quality_metrics = []
        start_time = time.time()

        was_capturing = self._status == MicrophoneStatus.CAPTURING

        if not was_capturing:
            await self.start_capture()

        try:
            while time.time() - start_time < duration:
                if self._active_backend:
                    chunk = await self._active_backend.read_chunk()
                    if chunk is not None:
                        chunks.append(chunk)
                        if include_quality:
                            quality_metrics.append(
                                self._quality_analyzer.analyze(chunk)
                            )
                await asyncio.sleep(0.001)
        finally:
            if not was_capturing:
                await self.stop_capture()

        if chunks:
            audio_data = np.concatenate(chunks)
        else:
            audio_data = np.array([], dtype=np.float32)

        if include_quality:
            return audio_data, quality_metrics
        return audio_data

    async def capture_with_vad(
        self,
        max_duration: float = 10.0,
        min_duration: float = 0.5,
        silence_duration: Optional[float] = None,
    ) -> Tuple[np.ndarray, bool]:
        """
        Capture audio with voice activity detection.

        Returns:
            Tuple of (audio_array, speech_detected)
        """
        silence_duration = silence_duration or self.config.vad_silence_duration_s

        chunks = []
        start_time = time.time()
        is_speaking = False
        silence_start = None
        speech_detected = False

        was_capturing = self._status == MicrophoneStatus.CAPTURING

        if not was_capturing:
            await self.start_capture()

        try:
            while time.time() - start_time < max_duration:
                if self._active_backend:
                    chunk = await self._active_backend.read_chunk()

                    if chunk is not None:
                        quality = self._quality_analyzer.analyze(chunk)
                        threshold = self._quality_analyzer.get_adaptive_threshold()

                        if quality.rms_energy > threshold:
                            is_speaking = True
                            speech_detected = True
                            silence_start = None
                            chunks.append(chunk)
                        elif is_speaking:
                            chunks.append(chunk)

                            if silence_start is None:
                                silence_start = time.time()
                            elif time.time() - silence_start > silence_duration:
                                elapsed = time.time() - start_time
                                if elapsed >= min_duration:
                                    break

                await asyncio.sleep(0.001)

        finally:
            if not was_capturing:
                await self.stop_capture()

        if chunks:
            audio_data = np.concatenate(chunks)
        else:
            audio_data = np.array([], dtype=np.float32)

        return audio_data, speech_detected

    async def stream_audio(self) -> AsyncIterator[Tuple[np.ndarray, AudioQualityMetrics]]:
        """
        Async generator for streaming audio chunks with quality metrics.

        Yields:
            Tuples of (audio_chunk, quality_metrics)
        """
        was_capturing = self._status == MicrophoneStatus.CAPTURING

        if not was_capturing:
            await self.start_capture()

        try:
            while self._status == MicrophoneStatus.CAPTURING:
                if self._active_backend:
                    chunk = await self._active_backend.read_chunk()
                    if chunk is not None:
                        quality = self._quality_analyzer.analyze(chunk)
                        yield chunk, quality
                await asyncio.sleep(0.001)
        finally:
            if not was_capturing:
                await self.stop_capture()

    def on_audio(
        self,
        callback: Callable[[np.ndarray, AudioQualityMetrics], None]
    ) -> None:
        """Register callback for audio chunks."""
        self._on_audio_callbacks.append(callback)

    def on_status_change(
        self,
        callback: Callable[[MicrophoneStatus], None]
    ) -> None:
        """Register callback for status changes."""
        self._on_status_change_callbacks.append(callback)

    def on_error(
        self,
        callback: Callable[[MicrophoneError], None]
    ) -> None:
        """Register callback for errors."""
        self._on_error_callbacks.append(callback)

    def get_stats(self) -> Dict[str, Any]:
        """Get microphone manager statistics."""
        uptime = None
        if self._stats["start_time"]:
            uptime = (datetime.now() - self._stats["start_time"]).total_seconds()

        return {
            "status": self._status.name,
            "active_backend": self.active_backend_type.value,
            "uptime_seconds": uptime,
            "total_chunks_captured": self._stats["total_chunks_captured"],
            "total_errors": self._stats["total_errors"],
            "consecutive_errors": self._consecutive_errors,
            "backend_switches": self._stats["backend_switches"],
            "recovery_attempts": self._stats["recovery_attempts"],
            "successful_recoveries": self._stats["successful_recoveries"],
            "noise_floor": self._quality_analyzer.noise_floor,
            "calibrated": self._quality_analyzer.calibrated,
        }

    async def cleanup(self) -> None:
        """Clean up all resources."""
        self._set_status(MicrophoneStatus.SHUTDOWN)

        await self.stop_capture()

        for backend in self._backends.values():
            await backend.cleanup()

        self._backends.clear()
        self._active_backend = None
        self._on_audio_callbacks.clear()
        self._on_status_change_callbacks.clear()
        self._on_error_callbacks.clear()

        logger.info("MicrophoneManager cleanup complete")

    async def check_permission(self) -> bool:
        """Check microphone permission status."""
        return await self._permission_handler.check_permission()

    async def request_permission(self) -> bool:
        """Request microphone permission."""
        return await self._permission_handler.request_permission()

    async def open_permission_settings(self) -> bool:
        """Open system permission settings."""
        return await self._permission_handler.open_permission_settings()


# =============================================================================
# Convenience Functions
# =============================================================================

async def get_microphone_manager(
    config: Optional[MicrophoneConfig] = None
) -> MicrophoneManager:
    """Get the global microphone manager instance."""
    manager = MicrophoneManager.get_instance(config)
    if manager.status == MicrophoneStatus.INITIALIZING:
        await manager.initialize()
    return manager


# =============================================================================
# Legacy Compatibility
# =============================================================================

# Keep AudioConfig for backward compatibility
@dataclass
class AudioConfig:
    """Legacy audio configuration (use MicrophoneConfig instead)."""
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1024
    format: int = 8  # pyaudio.paInt16

    @property
    def bytes_per_sample(self) -> int:
        return 2  # int16


class AudioCapture:
    """
    Legacy AudioCapture class for backward compatibility.
    Wraps the new MicrophoneManager.
    """

    def __init__(self, config: Optional[AudioConfig] = None):
        self._legacy_config = config or AudioConfig()
        self._manager: Optional[MicrophoneManager] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Convert legacy config
        mic_config = MicrophoneConfig()
        mic_config.sample_rate = self._legacy_config.sample_rate
        mic_config.channels = self._legacy_config.channels
        mic_config.chunk_duration_ms = int(
            self._legacy_config.chunk_size / self._legacy_config.sample_rate * 1000
        )

        self.config = self._legacy_config
        self._mic_config = mic_config
        self.callbacks = []
        self.noise_floor = None
        self.calibrated = False

    def _get_manager(self) -> MicrophoneManager:
        """Get or create manager instance."""
        if self._manager is None:
            self._manager = MicrophoneManager.get_instance(self._mic_config)
        return self._manager

    def _run_async(self, coro):
        """Run async code in sync context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(coro)

    def add_callback(self, callback: Callable[[np.ndarray], None]):
        """Add callback for audio chunks."""
        self.callbacks.append(callback)

        # Register with manager
        def wrapper(audio: np.ndarray, quality: AudioQualityMetrics):
            callback(audio)

        self._get_manager().on_audio(wrapper)

    def calibrate_noise_floor(self, duration: float = 1.0) -> float:
        """Calibrate noise floor."""
        manager = self._get_manager()

        async def _calibrate():
            await manager.initialize()
            await manager.start_capture()
            noise_floor = await manager.calibrate_noise(duration)
            await manager.stop_capture()
            return noise_floor

        self.noise_floor = self._run_async(_calibrate())
        self.calibrated = True
        return self.noise_floor

    def capture_audio(self, duration: float, silent: bool = False) -> np.ndarray:
        """Capture audio for specified duration."""
        manager = self._get_manager()

        async def _capture():
            await manager.initialize()
            return await manager.capture_audio(duration)

        if not silent:
            logger.info(f"Starting audio capture for {duration} seconds")

        return self._run_async(_capture())

    def capture_with_vad(
        self,
        max_duration: float = 10.0,
        silence_threshold: float = 0.02,
        silence_duration: float = 1.5
    ) -> Tuple[np.ndarray, bool]:
        """Capture audio with VAD."""
        manager = self._get_manager()

        async def _capture():
            await manager.initialize()
            return await manager.capture_with_vad(
                max_duration=max_duration,
                silence_duration=silence_duration,
            )

        return self._run_async(_capture())

    def list_devices(self) -> List[Dict]:
        """List available audio devices."""
        manager = self._get_manager()

        async def _list():
            await manager.initialize()
            devices = await manager.list_devices()
            return [d.to_dict() for d in devices]

        return self._run_async(_list())

    def start_continuous_capture(self):
        """Start continuous capture (legacy)."""
        manager = self._get_manager()
        self._run_async(manager.initialize())
        self._run_async(manager.start_capture())

    def stop_continuous_capture(self):
        """Stop continuous capture (legacy)."""
        manager = self._get_manager()
        self._run_async(manager.stop_capture())

    def get_audio_chunk(self, timeout: float = 0.1) -> Optional[np.ndarray]:
        """Get audio chunk (legacy)."""
        manager = self._get_manager()

        async def _get():
            if manager._active_backend:
                return await manager._active_backend.read_chunk()
            return None

        return self._run_async(_get())

    def __del__(self):
        """Cleanup resources."""
        if self._manager:
            try:
                self._run_async(self._manager.cleanup())
            except Exception:
                pass


class AudioVisualizer:
    """Real-time audio visualization for enrollment UI."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.history_size = 100
        self.energy_history = []
        self.pitch_history = []

    def update(self, audio_chunk: np.ndarray) -> dict:
        """Update visualization data."""
        # Ensure float32
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)
            if audio_chunk.max() > 1.0:
                audio_chunk = audio_chunk / 32768.0

        # Calculate energy
        energy = float(np.sqrt(np.mean(audio_chunk ** 2)))
        self.energy_history.append(energy)
        if len(self.energy_history) > self.history_size:
            self.energy_history.pop(0)

        # Simple pitch estimation (zero-crossing rate)
        zero_crossings = np.sum(np.abs(np.diff(np.sign(audio_chunk)))) / 2
        pitch_estimate = zero_crossings * self.sample_rate / (2 * len(audio_chunk))
        self.pitch_history.append(pitch_estimate)
        if len(self.pitch_history) > self.history_size:
            self.pitch_history.pop(0)

        # Calculate spectrum
        spectrum = np.abs(np.fft.rfft(audio_chunk * np.hanning(len(audio_chunk))))
        spectrum_db = 20 * np.log10(spectrum + 1e-10)

        return {
            'energy': energy,
            'energy_history': list(self.energy_history),
            'pitch_estimate': pitch_estimate,
            'pitch_history': list(self.pitch_history),
            'spectrum': spectrum_db[:50].tolist(),
            'waveform': audio_chunk[::10].tolist(),
        }


# =============================================================================
# Testing
# =============================================================================

async def test_microphone_manager():
    """Test the microphone manager."""
    print("=" * 60)
    print("Testing Unified Microphone Manager")
    print("=" * 60)

    # Get manager
    manager = await get_microphone_manager()

    # Check permission
    print("\n1. Checking permission...")
    has_permission = await manager.check_permission()
    print(f"   Permission: {'granted' if has_permission else 'denied'}")

    if not has_permission:
        print("   Requesting permission...")
        await manager.request_permission()

    # List devices
    print("\n2. Listing devices...")
    devices = await manager.list_devices()
    for device in devices:
        default = " (default)" if device.is_default else ""
        print(f"   [{device.index}] {device.name}{default}")

    # Calibrate noise
    print("\n3. Calibrating noise floor (please be quiet)...")
    noise_floor = await manager.calibrate_noise(1.0)
    print(f"   Noise floor: {noise_floor:.6f}")

    # Capture audio
    print("\n4. Capturing 3 seconds of audio (please speak)...")
    audio, metrics = await manager.capture_audio(3.0, include_quality=True)
    print(f"   Captured {len(audio)} samples")
    print(f"   Duration: {len(audio)/manager.config.sample_rate:.2f}s")
    if metrics:
        avg_energy = np.mean([m.rms_energy for m in metrics])
        print(f"   Avg energy: {avg_energy:.6f}")

    # Test VAD capture
    print("\n5. Testing VAD capture (speak then pause)...")
    vad_audio, detected = await manager.capture_with_vad(max_duration=5.0)
    print(f"   Speech detected: {detected}")
    print(f"   Captured {len(vad_audio)} samples")

    # Show stats
    print("\n6. Statistics:")
    stats = manager.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")

    # Cleanup
    await manager.cleanup()
    print("\nTest complete!")


def test_audio_capture():
    """Legacy test function for AudioCapture compatibility."""
    print("Testing audio capture (legacy interface)...")

    capture = AudioCapture()

    # List devices
    print("\nAvailable audio devices:")
    for device in capture.list_devices():
        print(f"  {device['index']}: {device['name']} ({device['channels']} channels)")

    # Test calibration
    print("\n1. Calibrating noise floor (please be quiet)...")
    noise_level = capture.calibrate_noise_floor()
    print(f"   Noise floor: {noise_level:.4f}")

    # Test fixed duration capture
    print("\n2. Testing 3-second capture (please speak)...")
    audio = capture.capture_audio(3.0)
    print(f"   Captured {len(audio)/capture.config.sample_rate:.2f} seconds of audio")
    print(f"   Energy level: {np.sqrt(np.mean(audio**2)):.4f}")

    # Test VAD capture
    print("\n3. Testing VAD capture (speak, then pause for 1.5 seconds)...")
    audio_vad, detected = capture.capture_with_vad(max_duration=10.0)
    if detected:
        print(f"   Captured {len(audio_vad)/capture.config.sample_rate:.2f} seconds with VAD")
    else:
        print("   No speech detected")

    print("\nAudio capture test complete!")


if __name__ == "__main__":
    import sys

    if "--legacy" in sys.argv:
        test_audio_capture()
    else:
        asyncio.run(test_microphone_manager())
