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
import atexit
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

from backend.audio.playback_ring_buffer import PlaybackRingBuffer

logger = logging.getLogger(__name__)


_AUDIO_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="jarvis-audio-io",
)


@atexit.register
def _shutdown_audio_io_executor() -> None:
    """Release the dedicated audio executor during interpreter shutdown."""
    try:
        _AUDIO_IO_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


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
    startup_silence_ms: int = 250         # Silence window to avoid startup pop/click/static

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
        self._cancel_requested = threading.Event()  # v278.2: cancellation flag for executor thread
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

    async def start(
        self,
        progress_callback=None,
        profile_strategy: str = "balanced",
    ) -> None:
        """
        Open the full-duplex stream.

        v266.4: ALL PortAudio operations (device validation, profile checks,
        stream creation, stream start) are synchronous C calls into CoreAudio
        on macOS. Running these on the event loop freezes it — defeating
        asyncio.wait_for() timeouts. Same root-cause pattern as ECAPA v265.2
        (speechbrain import) and Zone 6 v265.2 (chromadb import).

        Fix: run the entire synchronous PortAudio initialization in a thread
        executor so the event loop stays responsive and timeouts actually fire.

        v275.6: Added progress_callback(phase: str, detail: str) for init
        progress heartbeats. Called at each CoreAudio step so callers can
        distinguish hangs from slow progress on timeout.
        """
        if self._running:
            logger.warning("[FullDuplexDevice] Already running")
            return

        self._cancel_requested.clear()  # v278.2: reset for fresh init

        if sd is None:
            raise ImportError("sounddevice is not installed")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _AUDIO_IO_EXECUTOR,
            lambda: self._open_stream_sync(
                progress_callback,
                profile_strategy=profile_strategy,
            ),
        )

        # These touch asyncio primitives — must stay on event loop thread.
        self._started_event.set()

    def _open_stream_sync(
        self,
        progress_callback=None,
        *,
        profile_strategy: str = "balanced",
    ) -> None:
        """
        Synchronous PortAudio initialization — runs in thread executor.

        Handles device validation, profile building, stream creation and start.
        On success, sets self._running = True and populates self._stream.
        On failure, raises RuntimeError with details.

        v275.6: progress_callback(phase, detail) emits heartbeats at each
        CoreAudio step for timeout classification (hung vs slow).

        v279.0: Removed redundant _profile_supported() preflight checks.
        Each sd.check_*_settings() call is a synchronous CoreAudio C call
        taking 1-3s on macOS. With 4 profiles × 2 checks each, the preflight
        phase alone consumed 8-12s of the 15s timeout budget — causing the
        AudioBus init timeout and cascading zombie/recovery/static issues.
        The stream constructor (_create_stream) already validates settings
        internally and raises on incompatible profiles, so the preflight
        checks were purely redundant overhead. Try-first, catch on failure.
        """
        def _pcb(phase, detail):
            if progress_callback is not None:
                try:
                    progress_callback(phase, detail)
                except Exception:
                    pass  # never let callback failure abort audio init

        _pcb("device_query", "started")
        try:
            if os.getenv("JARVIS_AUDIO_VALIDATE_DEVICES", "true").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                self._validate_device_selection()
        except Exception:
            raise
        _pcb("device_query", "completed")

        startup_profiles = self._build_startup_profiles(profile_strategy=profile_strategy)
        startup_errors: List[str] = []

        for idx, profile in enumerate(startup_profiles, start=1):
            sample_rate = int(profile["sample_rate"])
            mode = str(profile["mode"])
            input_enabled = bool(profile["input_enabled"])

            self._input_enabled = input_enabled
            self._mode = mode
            self.config.sample_rate = sample_rate

            _pcb("profile_try", f"profile_{idx}")

            try:
                if self._cancel_requested.is_set():
                    self._safe_close_stream()
                    raise RuntimeError("[FullDuplexDevice] Init cancelled by caller")

                _pcb("stream_open", f"profile_{idx}")
                self._stream = self._create_stream(
                    mode=mode,
                    sample_rate=sample_rate,
                )

                if self._cancel_requested.is_set():
                    self._safe_close_stream()
                    raise RuntimeError("[FullDuplexDevice] Init cancelled by caller")

                _pcb("stream_start", f"profile_{idx}")
                self._stream.start()

                if self._cancel_requested.is_set():
                    logger.warning("[FullDuplexDevice] Init cancelled after stream started — aborting")
                    self._safe_close_stream()
                    raise RuntimeError("[FullDuplexDevice] Init cancelled (post-start cleanup)")

                self._running = True
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
                self._persist_successful_profile(
                    {
                        "mode": mode,
                        "input_enabled": input_enabled,
                        "sample_rate": sample_rate,
                    }
                )
                _pcb("stream_start", "completed")
                return
            except Exception as e:
                startup_errors.append(
                    f"profile#{idx} failed (mode={mode}, sr={sample_rate}): {e}"
                )
                self._safe_close_stream()
                continue

        self._running = False
        joined_errors = " | ".join(startup_errors[-5:])
        raise RuntimeError(
            "[FullDuplexDevice] Failed to start audio stream "
            f"after {len(startup_profiles)} profile(s): {joined_errors}"
        )

    def _build_startup_profiles(self, *, profile_strategy: str = "balanced") -> List[dict]:
        """
        Build deterministic startup profiles.

        Order:
        1) Last known-good profile for the resolved device pair.
        2) Preferred mode/sample-rate from config validation.
        3) Output-only fallback when duplex startup fails.
        4) Retry with output device default sample-rate (recovery/balanced only).
        """
        output_default_sr = self._get_output_default_sample_rate()
        profiles: List[dict] = []
        profile_strategy = (profile_strategy or "balanced").lower()

        def _add_profile(mode: str, input_enabled: bool, sample_rate: Optional[int]) -> None:
            if sample_rate is None:
                return
            try:
                resolved_sr = int(sample_rate)
            except Exception:
                return
            if resolved_sr <= 0:
                return
            profile = {
                "mode": mode,
                "input_enabled": input_enabled,
                "sample_rate": resolved_sr,
            }
            if profile not in profiles:
                profiles.append(profile)

        preferred_mode = "duplex" if self._input_enabled else "output-only"
        preferred_profile = {
            "mode": preferred_mode,
            "input_enabled": self._input_enabled,
            "sample_rate": self.config.sample_rate,
        }
        cached_profile = self._load_cached_profile()

        if (
            cached_profile is not None
            and str(cached_profile.get("mode")) == preferred_mode
        ):
            _add_profile(
                str(cached_profile.get("mode", preferred_mode)),
                bool(cached_profile.get("input_enabled", self._input_enabled)),
                cached_profile.get("sample_rate"),
            )

        _add_profile(
            preferred_profile["mode"],
            preferred_profile["input_enabled"],
            preferred_profile["sample_rate"],
        )

        if (
            cached_profile is not None
            and str(cached_profile.get("mode")) != preferred_mode
        ):
            _add_profile(
                str(cached_profile.get("mode", "duplex")),
                bool(cached_profile.get("input_enabled", True)),
                cached_profile.get("sample_rate"),
            )

        if self.config.allow_output_only and self._input_enabled:
            _add_profile("output-only", False, self.config.sample_rate)

        if profile_strategy not in ("startup", "fast-startup"):
            _add_profile(preferred_mode, self._input_enabled, output_default_sr)
            if self.config.allow_output_only:
                _add_profile("output-only", False, output_default_sr)

        max_profiles = {
            "fast-startup": 1,
            "startup": 2,
            "balanced": 4,
            "recovery": 4,
        }.get(profile_strategy, 4)
        if len(profiles) > max_profiles:
            profiles = profiles[:max_profiles]
        return profiles

    def _profile_cache_path(self) -> Path:
        """Resolve the device-profile cache file path."""
        path = os.getenv("JARVIS_AUDIO_PROFILE_CACHE_PATH")
        if path:
            return Path(path).expanduser()
        return Path.home() / ".jarvis" / "audio" / "startup_profile.json"

    def _device_signature(self) -> str:
        """Stable signature for the resolved device pair and channel policy."""
        input_device = (
            self.config.input_device
            if self._input_enabled and self.config.input_device is not None
            else "none"
        )
        output_device = (
            self.config.output_device
            if self.config.output_device is not None
            else "default"
        )
        return (
            f"in={input_device}|out={output_device}|"
            f"channels={self.config.channels}|dtype={self.config.dtype}"
        )

    def _load_cached_profile(self) -> Optional[dict]:
        """Load the last known-good profile for the resolved device pair."""
        cache_path = self._profile_cache_path()
        try:
            payload = json.loads(cache_path.read_text())
        except Exception:
            return None

        if payload.get("device_signature") != self._device_signature():
            return None

        sample_rate = payload.get("sample_rate")
        mode = str(payload.get("mode", ""))
        input_enabled = bool(payload.get("input_enabled", mode != "output-only"))
        if mode not in ("duplex", "output-only"):
            return None
        if input_enabled and not self._input_enabled:
            return None
        try:
            sample_rate = int(sample_rate)
        except Exception:
            return None
        if sample_rate <= 0:
            return None
        return {
            "mode": mode,
            "input_enabled": input_enabled,
            "sample_rate": sample_rate,
        }

    def _persist_successful_profile(self, profile: dict) -> None:
        """Persist the last known-good startup profile for this device pair."""
        cache_path = self._profile_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "device_signature": self._device_signature(),
                        "mode": profile.get("mode"),
                        "input_enabled": bool(profile.get("input_enabled", True)),
                        "sample_rate": int(profile.get("sample_rate")),
                        "updated_at": time.time(),
                    }
                )
            )
        except Exception:
            logger.debug("[FullDuplexDevice] Failed to persist startup profile", exc_info=True)

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
        """Best-effort cleanup. Uses abort() + active-polling for IO thread safety.

        v278.2: Uses Pa_AbortStream (immediate) instead of Pa_StopStream (waits
        for buffers). Polls stream.active with 2s hard cap to confirm the
        CoreAudio IO thread has fully exited before close(). This prevents
        SIGSEGV from callbacks accessing freed data structures.
        """
        stream = self._stream
        self._stream = None  # v278.2: Atomic swap prevents double-close from concurrent callers
        if stream is None:
            return
        self._running = False
        try:
            stream.abort()
        except Exception:
            pass
        # Wait for IO thread to fully exit (bounded 2s)
        _deadline = time.monotonic() + 2.0
        while time.monotonic() < _deadline:
            try:
                if not stream.active:
                    break
            except Exception:
                break
            time.sleep(0.005)
        try:
            stream.close()
        except Exception:
            pass

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
        """Close the stream and release resources.

        v278.2: Delegates to _safe_close_stream() in executor. The abort+wait
        inside _safe_close_stream() uses time.sleep for IO thread polling, so
        it must run off the event loop. Sets _cancel_requested first to
        short-circuit any in-flight _open_stream_sync() in another executor.
        """
        if not self._running and self._stream is None:
            return
        self._cancel_requested.set()
        self._running = False
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_AUDIO_IO_EXECUTOR, self._safe_close_stream)
        self._started_event.clear()
        self._startup_silence_frames = 0
        logger.info("[FullDuplexDevice] Stopped")

    def request_cancel(self) -> None:
        """Signal the executor thread to abort initialization.

        v278.2: Called by AudioBus when asyncio.wait_for() times out or
        CancelledError propagates. The executor thread checks this flag
        at 3 points in _open_stream_sync().
        """
        self._cancel_requested.set()

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
        # v278.2: Defensive guard — output silence when device is shutting down.
        # Prevents accessing freed/GC'd structures after stop() clears state.
        if not self._running:
            outdata[:] = 0.0
            return

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
        # v278.2: Defensive guard — output silence when device is shutting down.
        if not self._running:
            outdata[:] = 0.0
            return

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
