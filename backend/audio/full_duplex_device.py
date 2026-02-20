"""
Full-Duplex Audio Device Abstraction (Layer -1)
=================================================

Single sounddevice.Stream with synchronized capture and playback at the same
sample clock. This is the foundation for acoustic echo cancellation — both
indata (mic) and outdata (speaker) arrive in the same callback at the same
frame boundary, guaranteeing time-alignment.

CONSTRAINT: This is the ONLY place in the codebase that opens a sounddevice
stream. All audio I/O flows through this handle.

Architecture:
    Microphone ──▶ indata ──▶ capture_callback(np.ndarray)
                               (registered by AudioBus)
    outdata ◀── PlaybackRingBuffer ◀── write_playback(np.ndarray)
                                        (called by AudioBus)
"""

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

from backend.audio.playback_ring_buffer import PlaybackRingBuffer

logger = logging.getLogger(__name__)


@dataclass
class DeviceConfig:
    """Audio device configuration. All values can be overridden via env vars."""

    input_device: Optional[int] = None    # None = system default mic
    output_device: Optional[int] = None   # None = system default speaker
    sample_rate: int = 48000              # Native macOS rate
    internal_rate: int = 16000            # VAD/STT processing rate
    frame_duration_ms: int = 20           # 960 samples at 48kHz
    channels: int = 1
    dtype: str = "float32"
    playback_buffer_seconds: float = 2.0  # Ring buffer capacity
    require_input: bool = False           # Fail startup if no input device
    allow_output_only: bool = True        # Degrade to output-only when no input
    startup_silence_ms: int = 120         # Silence window to avoid startup pop/click

    def __post_init__(self):
        # Allow env var overrides
        self.sample_rate = int(os.getenv(
            "JARVIS_AUDIO_SAMPLE_RATE", str(self.sample_rate)
        ))
        self.internal_rate = int(os.getenv(
            "JARVIS_AUDIO_INTERNAL_RATE", str(self.internal_rate)
        ))
        self.frame_duration_ms = int(os.getenv(
            "JARVIS_AUDIO_FRAME_MS", str(self.frame_duration_ms)
        ))
        self.playback_buffer_seconds = float(os.getenv(
            "JARVIS_AUDIO_BUFFER_SECONDS", str(self.playback_buffer_seconds)
        ))
        self.startup_silence_ms = int(os.getenv(
            "JARVIS_AUDIO_STARTUP_SILENCE_MS", str(self.startup_silence_ms)
        ))
        require_input_env = os.getenv("JARVIS_AUDIO_REQUIRE_INPUT")
        if require_input_env is not None:
            self.require_input = require_input_env.lower() in (
                "1", "true", "yes", "on"
            )
        allow_output_only_env = os.getenv("JARVIS_AUDIO_ALLOW_OUTPUT_ONLY")
        if allow_output_only_env is not None:
            self.allow_output_only = allow_output_only_env.lower() in (
                "1", "true", "yes", "on"
            )

        dev_in = os.getenv("JARVIS_AUDIO_INPUT_DEVICE")
        if dev_in is not None:
            self.input_device = int(dev_in)

        dev_out = os.getenv("JARVIS_AUDIO_OUTPUT_DEVICE")
        if dev_out is not None:
            self.output_device = int(dev_out)

    @property
    def frame_size(self) -> int:
        """Samples per frame at device sample rate."""
        return int(self.sample_rate * self.frame_duration_ms / 1000)

    @property
    def internal_frame_size(self) -> int:
        """Samples per frame at internal (processing) rate."""
        return int(self.internal_rate * self.frame_duration_ms / 1000)

    @property
    def playback_buffer_frames(self) -> int:
        """Total ring buffer capacity in samples."""
        return int(self.sample_rate * self.playback_buffer_seconds)


class FullDuplexDevice:
    """
    Single sounddevice.Stream — ALL audio I/O through this handle.

    The callback processes both input (mic) and output (speaker) in the same
    invocation, ensuring frame-level synchronization required for AEC.
    """

    def __init__(self, config: Optional[DeviceConfig] = None):
        self.config = config or DeviceConfig()
        self._stream: Optional[Any] = None
        self._playback_buffer = PlaybackRingBuffer(
            capacity_frames=self.config.playback_buffer_frames
        )

        # Capture callback — set by AudioBus
        self._capture_callbacks: List[Callable[[np.ndarray], None]] = []
        self._capture_lock = threading.Lock()

        # Last output frame for AEC reference
        self._last_output_frame = np.zeros(
            self.config.frame_size, dtype=np.float32
        )
        self._output_frame_lock = threading.Lock()

        self._running = False
        self._started_event = asyncio.Event()
        self._input_enabled = True
        self._mode = "duplex"
        self._startup_silence_frames = 0

    @property
    def sample_rate(self) -> int:
        return self.config.sample_rate

    @property
    def frame_size(self) -> int:
        return self.config.frame_size

    @property
    def playback_buffer(self) -> PlaybackRingBuffer:
        return self._playback_buffer

    async def start(self) -> None:
        """Open the full-duplex stream."""
        if self._running:
            logger.warning("[FullDuplexDevice] Already running")
            return

        if sd is None:
            raise ImportError("sounddevice is not installed")

        try:
            if os.getenv("JARVIS_AUDIO_VALIDATE_DEVICES", "true").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                self._validate_device_selection()
        except Exception:
            # Validation errors are terminal and should surface as-is.
            raise

        startup_profiles = self._build_startup_profiles()
        startup_errors: List[str] = []

        for idx, profile in enumerate(startup_profiles, start=1):
            sample_rate = int(profile["sample_rate"])
            mode = str(profile["mode"])
            input_enabled = bool(profile["input_enabled"])

            self._input_enabled = input_enabled
            self._mode = mode
            self.config.sample_rate = sample_rate

            if not self._profile_supported(
                mode=mode,
                sample_rate=sample_rate,
                input_device=self.config.input_device,
                output_device=self.config.output_device,
            ):
                startup_errors.append(
                    f"profile#{idx} unsupported (mode={mode}, sr={sample_rate})"
                )
                continue

            try:
                self._stream = self._create_stream(
                    mode=mode,
                    sample_rate=sample_rate,
                )
                self._stream.start()
                self._running = True
                self._started_event.set()
                self._startup_silence_frames = max(
                    0,
                    int(self.config.sample_rate * self.config.startup_silence_ms / 1000),
                )

                in_device_label = (
                    self.config.input_device
                    if self.config.input_device is not None and self._input_enabled
                    else "none"
                )
                out_device_label = (
                    self.config.output_device
                    if self.config.output_device is not None
                    else "default"
                )
                fallback_note = ""
                if idx > 1:
                    fallback_note = f", fallback_profile={idx}/{len(startup_profiles)}"
                logger.info(
                    f"[FullDuplexDevice] Started: sr={self.config.sample_rate}, "
                    f"frame={self.config.frame_size} samples "
                    f"({self.config.frame_duration_ms}ms), "
                    f"mode={self._mode}, "
                    f"in={in_device_label}, out={out_device_label}, "
                    f"startup_silence={self.config.startup_silence_ms}ms{fallback_note}"
                )
                return
            except Exception as e:
                startup_errors.append(
                    f"profile#{idx} failed (mode={mode}, sr={sample_rate}): {e}"
                )
                self._safe_close_stream()
                continue

        self._started_event.clear()
        self._running = False
        joined_errors = " | ".join(startup_errors[-5:])
        raise RuntimeError(
            "[FullDuplexDevice] Failed to start audio stream "
            f"after {len(startup_profiles)} profile(s): {joined_errors}"
        )

    def _build_startup_profiles(self) -> List[dict]:
        """
        Build deterministic startup profiles.

        Order:
        1) Preferred mode/sample-rate from config validation.
        2) Output-only fallback when duplex startup fails.
        3) Retry with output device default sample-rate.
        """
        output_default_sr = self._get_output_default_sample_rate()
        sample_rates: List[int] = []
        for sr in (self.config.sample_rate, output_default_sr):
            if sr is None:
                continue
            try:
                val = int(sr)
            except Exception:
                continue
            if val > 0 and val not in sample_rates:
                sample_rates.append(val)

        if not sample_rates:
            sample_rates = [int(self.config.sample_rate)]

        profiles: List[dict] = []
        for sr in sample_rates:
            profiles.append(
                {
                    "mode": "duplex" if self._input_enabled else "output-only",
                    "input_enabled": self._input_enabled,
                    "sample_rate": sr,
                }
            )
            if self.config.allow_output_only:
                output_only_profile = {
                    "mode": "output-only",
                    "input_enabled": False,
                    "sample_rate": sr,
                }
                if output_only_profile not in profiles:
                    profiles.append(output_only_profile)
        return profiles

    def _get_output_default_sample_rate(self) -> Optional[int]:
        """Return output device default sample rate, if available."""
        if sd is None:
            return None
        try:
            output_device = self.config.output_device
            if output_device is None:
                defaults = getattr(sd.default, "device", None)
                if isinstance(defaults, (tuple, list)) and len(defaults) >= 2:
                    output_device = defaults[1]
            if output_device is None:
                return None
            info = sd.query_devices(int(output_device))
            default_sr = info.get("default_samplerate")
            if default_sr is None:
                return None
            return max(1, int(float(default_sr)))
        except Exception:
            return None

    def _profile_supported(
        self,
        *,
        mode: str,
        sample_rate: int,
        input_device: Optional[int],
        output_device: Optional[int],
    ) -> bool:
        """Preflight-check stream settings to avoid noisy startup failures."""
        assert sd is not None
        try:
            sd.check_output_settings(
                device=output_device,
                channels=self.config.channels,
                samplerate=sample_rate,
                dtype=self.config.dtype,
            )
            if mode == "duplex":
                sd.check_input_settings(
                    device=input_device,
                    channels=self.config.channels,
                    samplerate=sample_rate,
                    dtype=self.config.dtype,
                )
            return True
        except Exception:
            return False

    def _create_stream(self, *, mode: str, sample_rate: int) -> Any:
        """Create a sounddevice stream for the given mode/profile."""
        blocksize = int(sample_rate * self.config.frame_duration_ms / 1000)
        blocksize = max(1, blocksize)
        if mode == "duplex":
            return sd.Stream(
                samplerate=sample_rate,
                blocksize=blocksize,
                device=(self.config.input_device, self.config.output_device),
                channels=self.config.channels,
                dtype=self.config.dtype,
                callback=self._audio_callback,
                finished_callback=self._stream_finished,
            )
        return sd.OutputStream(
            samplerate=sample_rate,
            blocksize=blocksize,
            device=self.config.output_device,
            channels=self.config.channels,
            dtype=self.config.dtype,
            callback=self._output_only_callback,
            finished_callback=self._stream_finished,
        )

    def _safe_close_stream(self) -> None:
        """Best-effort cleanup for partially opened streams."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
        except Exception:
            pass
        try:
            self._stream.close()
        except Exception:
            pass
        self._stream = None

    def _validate_device_selection(self) -> None:
        """
        Validate and resolve duplex device selection before opening the stream.

        This prevents PortAudio device=-1 startup failures and avoids partially
        initialized audio state that can manifest as startup noise/static.
        """
        assert sd is not None  # guarded by caller

        try:
            devices = sd.query_devices()
        except Exception as e:
            raise RuntimeError(f"Unable to query audio devices: {e}") from e

        if not devices:
            raise RuntimeError("No audio devices available")

        default_input = None
        default_output = None
        try:
            defaults = getattr(sd.default, "device", None)
            if isinstance(defaults, (tuple, list)) and len(defaults) >= 2:
                default_input = int(defaults[0]) if defaults[0] is not None else None
                default_output = int(defaults[1]) if defaults[1] is not None else None
        except Exception:
            default_input = None
            default_output = None

        output_device = self._resolve_device(
            devices=devices,
            configured=self.config.output_device,
            default=default_output,
            direction="output",
        )
        if output_device is None:
            raise RuntimeError("No valid output device available")

        input_device = self._resolve_device(
            devices=devices,
            configured=self.config.input_device,
            default=default_input,
            direction="input",
        )
        if input_device is None:
            if self.config.require_input:
                raise RuntimeError("No valid input device available (required)")
            if self.config.allow_output_only:
                self._input_enabled = False
                self._mode = "output-only"
                logger.warning(
                    "[FullDuplexDevice] No valid input device available; "
                    "starting output-only mode"
                )
            else:
                raise RuntimeError("No valid input device available")
        else:
            self._input_enabled = True
            self._mode = "duplex"

        self.config.input_device = input_device
        self.config.output_device = output_device

    def _resolve_device(
        self,
        *,
        devices: List[dict],
        configured: Optional[int],
        default: Optional[int],
        direction: str,
    ) -> Optional[int]:
        """Resolve first valid device index for input or output direction."""
        assert sd is not None  # guarded by caller
        if direction not in ("input", "output"):
            raise ValueError(f"Unsupported direction: {direction}")

        cap_key = "max_input_channels" if direction == "input" else "max_output_channels"

        candidates: List[int] = []
        seen = set()

        def _add_candidate(idx: Optional[int]) -> None:
            if idx is None:
                return
            try:
                val = int(idx)
            except Exception:
                return
            if val < 0 or val in seen:
                return
            seen.add(val)
            candidates.append(val)

        _add_candidate(configured)
        _add_candidate(default)
        for idx, dev in enumerate(devices):
            try:
                if int(dev.get(cap_key, 0)) >= self.config.channels:
                    _add_candidate(idx)
            except Exception:
                continue

        for candidate in candidates:
            try:
                if direction == "input":
                    sd.check_input_settings(
                        device=candidate,
                        channels=self.config.channels,
                        samplerate=self.config.sample_rate,
                        dtype=self.config.dtype,
                    )
                else:
                    sd.check_output_settings(
                        device=candidate,
                        channels=self.config.channels,
                        samplerate=self.config.sample_rate,
                        dtype=self.config.dtype,
                    )
                return candidate
            except Exception:
                continue
        return None

    async def stop(self) -> None:
        """Close the stream and release resources."""
        if not self._running:
            return

        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning(f"[FullDuplexDevice] Error stopping stream: {e}")
            self._stream = None

        self._started_event.clear()
        self._startup_silence_frames = 0
        logger.info("[FullDuplexDevice] Stopped")

    def add_capture_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        """Register a callback to receive mic frames (float32, device rate)."""
        with self._capture_lock:
            if cb not in self._capture_callbacks:
                self._capture_callbacks.append(cb)

    def remove_capture_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        """Unregister a capture callback."""
        with self._capture_lock:
            if cb in self._capture_callbacks:
                self._capture_callbacks.remove(cb)

    def write_playback(self, audio: np.ndarray) -> int:
        """
        Queue audio for playback. Returns number of frames written.
        Audio must be float32 at the device sample rate.
        """
        if not self._running:
            return 0
        return self._playback_buffer.write(audio)

    def flush_playback(self) -> int:
        """
        Immediately discard all queued playback audio.
        Used for barge-in interruption.

        Returns:
            Number of frames discarded.
        """
        return self._playback_buffer.flush()

    def get_last_output_frame(self) -> np.ndarray:
        """
        Get the last frame sent to the speaker. Used by AEC as reference signal.
        Thread-safe — can be called from any thread.
        """
        with self._output_frame_lock:
            return self._last_output_frame.copy()

    def _audio_callback(
        self,
        indata: np.ndarray,
        outdata: np.ndarray,
        frames: int,
        time_info: "sd.CallbackTimeInfo",
        status: "sd.CallbackFlags",
    ) -> None:
        """
        sounddevice stream callback — runs in audio thread.

        CRITICAL: This must be fast (<1ms). No allocations, no locks that
        could block, no I/O. The ring buffer read and callback dispatch
        are the only operations.
        """
        if status:
            logger.debug(f"[FullDuplexDevice] Stream status: {status}")

        # --- OUTPUT: Fill outdata from ring buffer ---
        out_flat = outdata[:, 0] if outdata.ndim == 2 else outdata
        silenced = self._apply_startup_silence(out_flat)
        if silenced < len(out_flat):
            self._playback_buffer.read(out_flat[silenced:])

        # Save output for AEC reference
        with self._output_frame_lock:
            self._last_output_frame = out_flat.copy()

        # --- INPUT: Dispatch mic frames to consumers ---
        in_flat = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()

        with self._capture_lock:
            for cb in self._capture_callbacks:
                try:
                    cb(in_flat)
                except Exception:
                    # Never let a consumer crash the audio thread
                    pass

    def _output_only_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: "sd.CallbackTimeInfo",
        status: "sd.CallbackFlags",
    ) -> None:
        """sounddevice output-only callback — playback path without mic capture."""
        if status:
            logger.debug(f"[FullDuplexDevice] Output stream status: {status}")

        out_flat = outdata[:, 0] if outdata.ndim == 2 else outdata
        silenced = self._apply_startup_silence(out_flat)
        if silenced < len(out_flat):
            self._playback_buffer.read(out_flat[silenced:])
        with self._output_frame_lock:
            self._last_output_frame = out_flat.copy()

    def _apply_startup_silence(self, out_flat: np.ndarray) -> int:
        """
        Zero the first N output samples after stream start.

        This prevents first-buffer speaker pop/static when CoreAudio takes a
        moment to fully transition into the running state.
        """
        remaining = self._startup_silence_frames
        if remaining <= 0:
            return 0
        to_silence = min(len(out_flat), remaining)
        if to_silence > 0:
            out_flat[:to_silence] = 0.0
            self._startup_silence_frames = remaining - to_silence
        return to_silence

    def _stream_finished(self) -> None:
        """Called when the stream finishes (e.g., device disconnected)."""
        logger.warning("[FullDuplexDevice] Stream finished unexpectedly")
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def get_device_info(self) -> dict:
        """Get info about the currently active audio devices."""
        if sd is None:
            return {"error": "sounddevice not available"}

        try:
            defaults = getattr(sd.default, "device", (None, None))
            default_output = (
                defaults[1]
                if isinstance(defaults, (tuple, list)) and len(defaults) >= 2
                else None
            )
            info = {
                "input": (
                    sd.query_devices(self.config.input_device)
                    if self.config.input_device is not None
                    else None
                ),
                "output": (
                    sd.query_devices(self.config.output_device)
                    if self.config.output_device is not None
                    else sd.query_devices(default_output) if default_output is not None else None
                ),
                "sample_rate": self.config.sample_rate,
                "frame_size": self.config.frame_size,
                "frame_duration_ms": self.config.frame_duration_ms,
                "running": self._running,
                "mode": self._mode,
                "input_enabled": self._input_enabled,
            }
            return info
        except Exception as e:
            return {"error": str(e)}

    async def wait_until_started(self, timeout: float = 5.0) -> bool:
        """Wait until the device stream is running."""
        try:
            await asyncio.wait_for(self._started_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def input_enabled(self) -> bool:
        """Whether microphone capture is active."""
        return self._input_enabled
