"""
AudioBus + Acoustic Echo Cancellation (Layer 0)
=================================================

Central audio routing singleton. Enforces single-speaker by construction.
Provides AEC-cleaned mic input to all consumers.

CONSTRAINT — not convention:
    The FullDuplexDevice callback is private to AudioBus. Nothing can bypass
    it. This is the ONLY way audio reaches speakers or leaves the microphone.

Architecture:
    Mic ──▶ FullDuplexDevice ──▶ AEC(mic, ref) ──▶ Resample 48→16k ──▶ consumers
                  ▲                                                        │
                  │                                                   (VAD/STT)
    Speaker ◀── PlaybackRingBuffer ◀── Resample 16→48k ◀── TTS output
"""

import asyncio
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, ClassVar, Dict, List, Optional

import numpy as np

from backend.audio.full_duplex_device import DeviceConfig, FullDuplexDevice

logger = logging.getLogger(__name__)


# ============================================================================
# Resampler (libsamplerate wrapper or fallback)
# ============================================================================

class Resampler:
    """
    High-quality audio resampler. Uses libsamplerate (samplerate package)
    when available, falls back to numpy linear interpolation.
    """

    def __init__(self, from_rate: int, to_rate: int, channels: int = 1):
        self.from_rate = from_rate
        self.to_rate = to_rate
        self.channels = channels
        self._ratio = to_rate / from_rate
        self._use_libsamplerate = False
        self._resampler = None
        #: Whether the backend accepts the third (end-of-input) argument.
        #: Probed once — never per frame.
        self._eoi_supported = False

        if from_rate == to_rate:
            return  # No-op

        try:
            import samplerate
            self._resampler = samplerate.Resampler("sinc_fastest", channels=channels)
            self._use_libsamplerate = True
            self._eoi_supported = self._probe_end_of_input()
            logger.debug(
                f"[Resampler] Using libsamplerate: {from_rate} -> {to_rate}"
            )
        except ImportError:
            logger.info(
                f"[Resampler] libsamplerate not available, using linear "
                f"interpolation: {from_rate} -> {to_rate}"
            )

    def _probe_end_of_input(self) -> bool:
        """Does this libsamplerate build accept the end-of-input flag?

        Answered ONCE, against a throwaway resampler so the real one's filter
        state is never perturbed by the probe. Bindings differ across versions;
        discovering that per-frame inside a try/except is what turned a
        signature drift into silent total frame loss."""
        try:
            import samplerate
            probe = samplerate.Resampler("sinc_fastest", channels=self.channels)
            probe.process(np.zeros(8, dtype=np.float32), 1.0, False)
            return True
        except Exception:  # noqa: BLE001
            logger.info(
                "[Resampler] backend does not accept an end-of-input flag; "
                "using the 2-argument form",
            )
            return False

    def process(
        self, data: np.ndarray, end_of_data: bool = False
    ) -> np.ndarray:
        """Resample audio data.

        Args:
            data: Input audio (float32).
            end_of_data: If True, tells libsamplerate this is the final
                chunk — flushes internal filter state so no trailing
                samples are held.  Use True for complete utterances,
                False for streaming chunks.

        v237.0: Added end_of_data to prevent trailing-sample loss.
        """
        if self.from_rate == self.to_rate:
            return data

        if self._use_libsamplerate and self._resampler is not None:
            # POSITIONAL, not keyword. This call was ``end_of_data=...`` while
            # libsamplerate's pybind11 binding declares ``end_of_input`` —
            # so every invocation raised TypeError, AudioBus._on_mic_frame
            # swallowed it at DEBUG level, and 100% of microphone frames were
            # discarded while every status surface reported healthy. One
            # keyword silenced the entire microphone.
            #
            # Positional is the durable form: an argument RENAME breaks
            # keywords silently, whereas a reorder would break every caller of
            # the library loudly. ``_eoi_supported`` is resolved ONCE at init,
            # because per-frame try/except in the audio path is precisely the
            # pattern that hid this for so long.
            if self._eoi_supported:
                return self._resampler.process(data, self._ratio, end_of_data)
            return self._resampler.process(data, self._ratio)

        # Fallback: linear interpolation (end_of_data N/A)
        n_out = int(len(data) * self._ratio)
        if n_out == 0:
            return np.zeros(0, dtype=np.float32)
        indices = np.linspace(0, len(data) - 1, n_out)
        return np.interp(indices, np.arange(len(data)), data).astype(np.float32)


# ============================================================================
# Acoustic Echo Canceller
# ============================================================================

#: How fast the observed-peak scale relaxes. ~0.9995 per 20ms frame is a few
#: seconds to halve — slow enough not to pump inside an utterance, fast enough
#: to recover when the operator stops shouting.
_RANGE_PEAK_DECAY = float(os.getenv("JARVIS_AUDIO_RANGE_DECAY", "0.9995"))

#: Soft-knee threshold. Below this the signal is bit-identical; above it the
#: curve begins. 0.75 leaves normal speech completely untouched (measured
#: speech peaks here run 0.2-0.6) while catching the excursions that saturate.
def _agc_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.getenv(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


#: Above this the frame is scaled. Below it the signal passes BIT-IDENTICAL —
#: the common case must cost nothing and change nothing.
_AGC_CEILING = _agc_env("JARVIS_AUDIO_AGC_CEILING", 0.90, 0.10, 0.99)

#: Where a scaled frame is placed. Under the ceiling, so the scaler is not
#: re-triggered by its own output on the next frame.
_AGC_TARGET = _agc_env("JARVIS_AUDIO_AGC_TARGET", 0.95, 0.10, 0.99)

#: Seconds for the gain to climb back toward unity once the loud passage
#: ends. Long, deliberately: speech has pauses, and a governor that recovered
#: inside one would raise the noise floor between every word — audible as
#: "pumping" and, worse, a moving noise floor for the endpointer to chase.
_AGC_RELEASE_S = _agc_env("JARVIS_AUDIO_AGC_RELEASE_S", 2.0, 0.05, 60.0)

#: Floor on the gain. Without it a single freak transient could attenuate the
#: whole session toward silence with no way back inside the release constant.
_AGC_MIN_GAIN = _agc_env("JARVIS_AUDIO_AGC_MIN_GAIN", 0.02, 1e-4, 1.0)

# --- upward normalization -------------------------------------------------
#
# Measured on this machine: the operator's speech arrives at peak 0.070,
# rms 0.0029 — 27x quieter than a played probe on the SAME microphone, and
# whisper cannot read it from the raw device tap. Scaling DOWN was never
# going to help that; the signal needs to come UP.

#: Below this peak the frame is a candidate for boosting.
_AGC_QUIET_PEAK = _agc_env("JARVIS_AUDIO_AGC_QUIET_PEAK", 0.10, 0.01, 0.90)

#: Where a boosted frame is placed. Comfortably below the ceiling so a
#: boosted transient cannot punch through it, and in the range whisper's
#: feature extractor is happiest with.
_AGC_NOMINAL = _agc_env("JARVIS_AUDIO_AGC_NOMINAL", 0.50, 0.05, 0.90)

#: Ceiling on boost. Without it, a silent room would be multiplied until its
#: noise floor filled the range — deafening static, and a VAD that fires on
#: everything.
_AGC_MAX_BOOST = _agc_env("JARVIS_AUDIO_AGC_MAX_BOOST", 24.0, 1.0, 200.0)

#: How far above the tracked noise floor a peak must sit before it is treated
#: as signal. Measured separation on this machine: speech peaks 0.070 against
#: an ambient floor of 0.0068 — 20dB. RMS separation was only 3.7dB, which is
#: why the gate keys on PEAK.
_AGC_SQUELCH_RATIO = _agc_env("JARVIS_AUDIO_AGC_SQUELCH_RATIO", 4.0, 1.1, 100.0)

#: Absolute floor. Below this a frame is silence in any room.
_AGC_SQUELCH_ABS = _agc_env("JARVIS_AUDIO_AGC_SQUELCH_ABS", 0.005, 1e-5, 0.5)

#: Noise-floor tracker time constants. Rises slowly (a room that gets louder
#: is learned over seconds) and falls quickly (a room that goes quiet must be
#: believed at once, or the gate stays shut through the next utterance).
_AGC_FLOOR_RISE_S = _agc_env("JARVIS_AUDIO_AGC_FLOOR_RISE_S", 8.0, 0.1, 120.0)
_AGC_FLOOR_FALL_S = _agc_env("JARVIS_AUDIO_AGC_FLOOR_FALL_S", 1.0, 0.05, 60.0)

_AGC_THRESHOLD = max(0.05, min(0.99, float(
    os.getenv("JARVIS_AUDIO_AGC_THRESHOLD", "0.75"),
)))


class AcousticEchoCanceller:
    """
    Wraps speexdsp for acoustic echo cancellation. Falls back to spectral
    subtraction if speexdsp is not available.

    The AEC operates at the internal processing rate (16kHz by default).
    """

    def __init__(self, frame_size: int, sample_rate: int = 16000, tail_ms: int = 200):
        self._frame_size = frame_size
        self._sample_rate = sample_rate
        self._tail_length = int(sample_rate * tail_ms / 1000)
        self._aec = None
        self._use_speexdsp = False

        try:
            import speexdsp
            self._aec = speexdsp.EchoCanceller(
                frame_size=frame_size,
                filter_length=self._tail_length,
                sample_rate=sample_rate,
            )
            self._use_speexdsp = True
            logger.info(
                f"[AEC] Using speexdsp: frame={frame_size}, "
                f"tail={tail_ms}ms, sr={sample_rate}"
            )
        except (ImportError, Exception) as e:
            logger.info(
                f"[AEC] speexdsp not available ({e}), using spectral "
                f"subtraction fallback"
            )

    def cancel_echo(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """
        Remove echo of the reference signal (speaker output) from the mic signal.

        Args:
            mic: Microphone capture (float32, internal rate)
            ref: Speaker output reference (float32, internal rate, same length)

        Returns:
            Echo-cancelled mic signal (float32).
        """
        if len(mic) == 0:
            return mic

        if self._use_speexdsp and self._aec is not None:
            try:
                # speexdsp expects int16
                mic_i16 = (mic * 32767).astype(np.int16)
                ref_i16 = (ref * 32767).astype(np.int16)

                # Pad or truncate ref to match mic length
                if len(ref_i16) < len(mic_i16):
                    ref_i16 = np.pad(
                        ref_i16, (0, len(mic_i16) - len(ref_i16))
                    )
                elif len(ref_i16) > len(mic_i16):
                    ref_i16 = ref_i16[:len(mic_i16)]

                out_i16 = self._aec.process(
                    mic_i16.tobytes(),
                    ref_i16.tobytes(),
                )
                return np.frombuffer(out_i16, dtype=np.int16).astype(np.float32) / 32767.0
            except Exception as e:
                logger.debug(f"[AEC] speexdsp error, falling back: {e}")

        # Fallback: spectral subtraction
        return self._spectral_subtraction(mic, ref)

    def _spectral_subtraction(
        self, mic: np.ndarray, ref: np.ndarray
    ) -> np.ndarray:
        """Simple spectral subtraction for echo reduction."""
        if len(ref) == 0 or np.max(np.abs(ref)) < 1e-6:
            return mic  # No reference signal, nothing to subtract

        # Pad ref to match mic
        if len(ref) < len(mic):
            ref = np.pad(ref, (0, len(mic) - len(ref)))
        elif len(ref) > len(mic):
            ref = ref[:len(mic)]

        n_fft = len(mic)
        mic_fft = np.fft.rfft(mic, n=n_fft)
        ref_fft = np.fft.rfft(ref, n=n_fft)

        # Estimate echo magnitude and subtract
        alpha = float(os.getenv("JARVIS_AEC_ALPHA", "1.0"))
        mic_mag = np.abs(mic_fft)
        ref_mag = np.abs(ref_fft) * alpha

        # Spectral floor to avoid musical noise
        floor = 0.01 * mic_mag
        cleaned_mag = np.maximum(mic_mag - ref_mag, floor)

        # Reconstruct with original phase
        phase = np.angle(mic_fft)
        cleaned_fft = cleaned_mag * np.exp(1j * phase)
        cleaned = np.fft.irfft(cleaned_fft, n=n_fft)

        return cleaned[:len(mic)].astype(np.float32)


# ============================================================================
# Audio Sink ABC + Implementations
# ============================================================================

class AudioSink(ABC):
    """Pluggable output target for audio."""

    @abstractmethod
    async def write(self, audio: np.ndarray, sample_rate: int) -> None:
        """Write audio to the sink."""


class LocalSpeakerSink(AudioSink):
    """Routes audio through the FullDuplexDevice playback path.

    v237.0: Paced chunked writes prevent ring-buffer overflow and
    silent data loss for utterances longer than the buffer capacity.
    Fixed elif branch that corrupted audio already at device rate.
    """

    # Polling interval while waiting for ring-buffer space (seconds).
    _PACE_POLL_INTERVAL: float = 0.005  # 5 ms

    def __init__(self, device: FullDuplexDevice, resampler: Resampler):
        self._device = device
        self._resampler = resampler
        self._edge_fade_ms = max(
            0.0,
            float(os.getenv("JARVIS_AUDIO_EDGE_FADE_MS", "6.0")),
        )
        self._output_headroom = min(
            1.0,
            max(0.1, float(os.getenv("JARVIS_AUDIO_OUTPUT_HEADROOM", "0.98"))),
        )

    def _condition_audio(self, audio: np.ndarray) -> np.ndarray:
        """
        Normalize and shape playback audio to prevent static/crackle artifacts.

        - Replaces NaN/inf samples with silence.
        - Enforces output headroom to avoid clipping distortion.
        - Applies a short edge fade to remove click/pops at utterance boundaries.
        """
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)
        if audio.size == 0:
            return audio

        np.nan_to_num(audio, copy=False, nan=0.0, posinf=0.0, neginf=0.0)

        peak = float(np.max(np.abs(audio)))
        if peak > self._output_headroom and peak > 0.0:
            audio = (audio / peak) * self._output_headroom

        np.clip(audio, -1.0, 1.0, out=audio)

        fade_samples = int(self._device.sample_rate * (self._edge_fade_ms / 1000.0))
        fade_samples = max(0, min(fade_samples, audio.size // 2))
        if fade_samples > 0:
            ramp = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
            audio[:fade_samples] *= ramp
            audio[-fade_samples:] *= ramp[::-1]

        return audio

    async def write(self, audio: np.ndarray, sample_rate: int) -> None:
        """Resample to device rate and queue for playback with pacing.

        v237.0 fixes:
        - Chunked writes with back-pressure so no audio is silently dropped.
        - Removed buggy elif that applied 16k→48k resampler to 48kHz audio.
        - Passes end_of_data=True to flush resampler filter state.
        """
        if sample_rate != self._device.sample_rate:
            # Create a one-shot resampler for this complete utterance
            temp_resampler = Resampler(sample_rate, self._device.sample_rate)
            audio = temp_resampler.process(audio, end_of_data=True)
        # v237.0: Removed the elif branch.  When sample_rate already equals
        # device_rate, NO resampling is needed — the old elif incorrectly
        # applied the 16k→48k up-resampler to already-48kHz audio, producing
        # garbled output ("hallucinations").

        # Ensure deterministic, artifact-resistant output shape.
        audio = self._condition_audio(audio)

        # --- Paced chunked write (v237.0) ---
        # Write in chunks, waiting for ring-buffer space between chunks.
        # This guarantees ALL audio reaches the speaker — no silent drops.
        offset = 0
        total = len(audio)
        max_wait_s = (total / self._device.sample_rate) + 10.0  # generous timeout
        import time as _time
        deadline = _time.monotonic() + max_wait_s

        while offset < total and self._device.is_running:
            free = self._device.playback_buffer.free_space
            if free <= 0:
                if _time.monotonic() > deadline:
                    logger.warning(
                        "[LocalSpeakerSink] Paced-write timeout — "
                        f"dropped {total - offset}/{total} frames"
                    )
                    break
                await asyncio.sleep(self._PACE_POLL_INTERVAL)
                continue

            chunk = audio[offset : offset + free]
            written = self._device.write_playback(chunk)
            if written <= 0:
                # Buffer unexpectedly full; yield and retry
                await asyncio.sleep(self._PACE_POLL_INTERVAL)
                continue
            offset += written


class WebSocketSink(AudioSink):
    """
    Streams audio to a browser client via WebSocket.
    AEC is CLIENT-side (browser WebRTC handles it).
    """

    def __init__(self, send_func: Callable):
        self._send = send_func

    async def write(self, audio: np.ndarray, sample_rate: int) -> None:
        """Send audio as int16 PCM bytes over WebSocket."""
        pcm_i16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        try:
            await self._send(pcm_i16.tobytes())
        except Exception as e:
            logger.debug(f"[WebSocketSink] Send error: {e}")


# ============================================================================
# AudioBus (Singleton)
# ============================================================================

class AudioBus:
    """
    Central audio routing bus. Singleton.

    ALL audio I/O in the system flows through this class.
    Provides AEC-cleaned mic input to registered consumers.
    Routes TTS output through the local speaker or WebSocket sinks.
    """

    _instance: ClassVar[Optional["AudioBus"]] = None
    # v266.4: MUST be RLock (re-entrant), not Lock.
    # get_instance() acquires this lock then calls cls() → __init__()
    # which also acquires it. threading.Lock deadlocks on same-thread
    # re-acquisition; RLock allows it. This was the root cause of the
    # startup hang introduced in v267.0.
    _creation_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self):
        # v267.0: Self-register as singleton on construction so that
        # ``get_instance_safe()`` always returns the live instance —
        # even when callers bypass ``get_instance()`` (which was the
        # root cause of startup static: the supervisor called AudioBus()
        # directly, leaving _instance = None, so the TTS pipeline
        # couldn't find the running bus and fell through to raw
        # afplay / sd.play(), opening a second audio stream and
        # producing device-contention static).
        with self._creation_lock:
            if AudioBus._instance is None:
                AudioBus._instance = self

        # These are set during start()
        self._device: Optional[FullDuplexDevice] = None
        #: Governor state. Declared here rather than discovered by getattr so
        #: the object's shape is honest and a reader can see the AGC exists.
        self._agc_gain: float = 1.0          # current linear gain, 1.0 = off
        self._range_peak: float = 0.0        # loudest input peak ever seen
        self._range_reports: int = 0         # log budget for over-scale input
        self._noise_floor: float = 0.0       # adaptive room-quiet estimate
        self._aec: Optional[AcousticEchoCanceller] = None
        self._resampler_down: Optional[Resampler] = None      # 48k -> 16k (mic)
        self._resampler_aec_ref: Optional[Resampler] = None  # 48k -> 16k (AEC ref)
        self._resampler_up: Optional[Resampler] = None       # 16k -> 48k
        self._config: Optional[DeviceConfig] = None

        # Mic consumers receive AEC-cleaned, 16kHz float32 frames
        self._mic_consumers: List[Callable[[np.ndarray], None]] = []
        self._consumer_lock = threading.Lock()

        # Output sinks
        self._sinks: Dict[str, AudioSink] = {}
        self._local_sink: Optional[LocalSpeakerSink] = None

        # State
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._playback_ready_at: float = 0.0  # monotonic timestamp when playback is safe

        # v265.0: Mic gate — when active, no mic frames are dispatched.
        # Used by speech state manager to suppress self-voice during TTS.
        self._mic_gate_active: bool = False
        #: Consecutive mic-frame processing failures. Non-zero means the
        #: microphone is not reaching its consumers, whatever else says.
        self._mic_error_count: int = 0
        self._mic_frames_delivered: int = 0
        #: Decaying observed peak, used to fit over-range input back into the
        #: normalized range every downstream consumer contracts on.
        self._range_peak: float = 1.0
        self._range_reports: int = 0

    @classmethod
    def get_instance(cls) -> "AudioBus":
        """Get or create the singleton AudioBus."""
        if cls._instance is None:
            with cls._creation_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def get_instance_safe(cls) -> Optional["AudioBus"]:
        """Get the singleton if it exists, otherwise None."""
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> Optional["AudioBus"]:
        """Clear singleton. Returns old instance for caller to stop().

        v278.2: Used after init timeout to ensure recovery creates a fresh
        instance instead of reusing the zombie singleton whose device may
        have an orphaned CoreAudio IO thread.
        """
        with cls._creation_lock:
            old = cls._instance
            cls._instance = None
            return old

    async def start(
        self,
        config: Optional[DeviceConfig] = None,
        progress_callback=None,
        profile_strategy: str = "balanced",
    ) -> None:
        """
        Initialize and start the audio device, AEC, and resamplers.

        v275.6: progress_callback is threaded to FullDuplexDevice.start()
        for init progress heartbeats.
        """
        if self._running:
            logger.warning("[AudioBus] Already running")
            return

        self._config = config or DeviceConfig()
        self._loop = asyncio.get_running_loop()

        # Initialize playback resampler (always required).
        self._resampler_up = Resampler(
            self._config.internal_rate, self._config.sample_rate
        )

        # Initialize audio device (duplex when possible, output-only fallback).
        # v278.2: Wrap in try/except to ensure cleanup on CancelledError or
        # any exception. Without this, asyncio.wait_for() cancels the task
        # but the executor thread in device.start() completes, launching a
        # CoreAudio IO thread with callbacks that access freed state → SIGSEGV.
        self._device = FullDuplexDevice(self._config)
        try:
            await self._device.start(
                progress_callback=progress_callback,
                profile_strategy=profile_strategy,
            )
        except (asyncio.CancelledError, Exception):
            logger.warning("[AudioBus] Device start interrupted — cleaning up")
            try:
                self._device.request_cancel()
                # v278.2: Synchronous cleanup — cannot await during CancelledError
                # propagation (the await would be immediately re-cancelled by the
                # task's cancelled state, making stop() a no-op for already-started
                # streams). _safe_close_stream() is bounded at 2s (abort + poll +
                # close) which is acceptable on the error path.
                self._device._safe_close_stream()
            except BaseException:
                pass
            self._device = None
            raise

        # Input processing is only enabled when capture is active.
        if self._device.input_enabled:
            self._resampler_down = Resampler(
                self._config.sample_rate, self._config.internal_rate
            )
            # v237.0: Separate resampler for AEC reference signal.
            # Using the SAME stateful resampler for both mic and reference
            # contaminated the internal filter state, degrading AEC quality.
            self._resampler_aec_ref = Resampler(
                self._config.sample_rate, self._config.internal_rate
            )
            self._aec = AcousticEchoCanceller(
                frame_size=self._config.internal_frame_size,
                sample_rate=self._config.internal_rate,
            )
            self._device.add_capture_callback(self._on_mic_frame)
        else:
            self._resampler_down = None
            self._resampler_aec_ref = None
            self._aec = None

        # Create local speaker sink
        self._local_sink = LocalSpeakerSink(self._device, self._resampler_up)
        self._sinks["local"] = self._local_sink

        self._running = True
        import time as _time_mod
        _settle_ms = max(0, int(os.getenv("JARVIS_AUDIO_POST_START_SETTLE_MS", "300")))
        self._playback_ready_at = _time_mod.monotonic() + (_settle_ms / 1000.0)
        logger.info(
            "[AudioBus] Started — all audio routing through bus "
            f"(mode={'duplex' if self._device.input_enabled else 'output-only'}, "
            f"settle={_settle_ms}ms)"
        )

    async def stop(self) -> None:
        """Stop the audio bus and release all resources.

        v278.2: Sets _running=False first so callbacks immediately no-op.
        Calls request_cancel() to short-circuit any in-flight executor.
        Clears self._device after stop to prevent double-use.
        """
        self._running = False
        device = self._device
        if device is not None:
            device.request_cancel()
            try:
                if device.input_enabled:
                    device.remove_capture_callback(self._on_mic_frame)
            except Exception:
                pass
            try:
                await device.stop()
            except Exception:
                pass
            self._device = None

        self._sinks.clear()
        self._local_sink = None

        with self._consumer_lock:
            self._mic_consumers.clear()

        logger.info("[AudioBus] Stopped")

    # ---- Output (TTS → Speaker) ----

    async def play_audio(
        self,
        audio: np.ndarray,
        sample_rate: int,
        sink_id: str = "local",
        wait_for_drain: bool = False,
    ) -> None:
        """
        Play a complete audio buffer through the specified sink.

        v237.0: Added *wait_for_drain* — when True the call blocks until
        the ring buffer has been fully consumed by the audio callback,
        ensuring callers know when playback actually finishes (not just
        when data is queued).  This is critical for correct speech-state
        tracking and prevents consecutive utterances from overflowing
        the ring buffer.

        Args:
            audio: float32 audio data
            sample_rate: Sample rate of the audio
            sink_id: Which sink to route to (default "local" speaker)
            wait_for_drain: If True, wait until the ring buffer empties
                before returning.
        """
        sink = self._sinks.get(sink_id)
        if sink is None:
            if self._local_sink is not None:
                sink = self._local_sink
            else:
                logger.warning(f"[AudioBus] No sink '{sink_id}' and no local sink")
                return

        # v279.0: Wait for CoreAudio to settle after fresh stream start.
        # Playing audio immediately after stream.start() produces static
        # because the CoreAudio IO thread needs time to fully initialize.
        import time as _time_mod
        settle_remaining = self._playback_ready_at - _time_mod.monotonic()
        if settle_remaining > 0:
            logger.debug(
                "[AudioBus] Waiting %.0fms for post-start settle",
                settle_remaining * 1000,
            )
            await asyncio.sleep(settle_remaining)

        await sink.write(audio, sample_rate)

        if wait_for_drain and self._device is not None:
            # Wait until the audio callback has fully consumed the buffer.
            # Generous timeout: expected drain time + 5 s headroom.
            import time as _time

            drain_timeout = (
                (self._device.playback_buffer.available / self._device.sample_rate)
                + 5.0
            )
            deadline = _time.monotonic() + drain_timeout
            while (
                self._running
                and self._device.is_running
                and self._device.playback_buffer.available > 0
            ):
                if _time.monotonic() > deadline:
                    logger.warning(
                        "[AudioBus] play_audio drain timeout — "
                        f"{self._device.playback_buffer.available} frames remain"
                    )
                    break
                await asyncio.sleep(0.010)  # 10 ms polling

    async def play_stream(
        self,
        chunks: AsyncIterator[np.ndarray],
        sample_rate: int,
        cancel: Optional[asyncio.Event] = None,
        sink_id: str = "local",
    ) -> None:
        """
        Stream audio chunks to the speaker. Stops if cancel event is set
        (barge-in).

        Args:
            chunks: Async iterator yielding float32 audio arrays
            sample_rate: Sample rate of the chunks
            cancel: Optional event to signal cancellation
            sink_id: Which sink to route to
        """
        sink = self._sinks.get(sink_id, self._local_sink)
        if sink is None:
            logger.warning("[AudioBus] No sink available for streaming")
            return

        async for chunk in chunks:
            if cancel is not None and cancel.is_set():
                logger.debug("[AudioBus] Stream cancelled (barge-in)")
                break
            await sink.write(chunk, sample_rate)

    def flush_playback(self) -> int:
        """
        Immediately discard all queued playback audio.
        Used for barge-in interruption. Thread-safe.

        Returns:
            Number of frames flushed.
        """
        if self._device is not None:
            return self._device.flush_playback()
        return 0

    # ---- Input (Mic → Consumers) ----

    def register_mic_consumer(self, cb: Callable[[np.ndarray], None]) -> None:
        """
        Register a callback to receive AEC-cleaned, 16kHz mic frames.

        The callback is invoked from the audio thread — it must be fast
        and non-blocking. Use a queue if processing is needed.
        """
        with self._consumer_lock:
            if cb not in self._mic_consumers:
                self._mic_consumers.append(cb)
                logger.debug(
                    f"[AudioBus] Registered mic consumer "
                    f"(total: {len(self._mic_consumers)})"
                )

    def unregister_mic_consumer(self, cb: Callable[[np.ndarray], None]) -> None:
        """Unregister a mic consumer."""
        with self._consumer_lock:
            if cb in self._mic_consumers:
                self._mic_consumers.remove(cb)

    # ---- Sink Management ----

    def register_sink(self, sink_id: str, sink: AudioSink) -> None:
        """Register a named audio output sink."""
        self._sinks[sink_id] = sink
        logger.debug(f"[AudioBus] Registered sink: {sink_id}")

    def unregister_sink(self, sink_id: str) -> None:
        """Unregister a named sink."""
        self._sinks.pop(sink_id, None)

    # ---- Mic Gating (v265.0) ----

    def set_mic_gate(self, active: bool) -> None:
        """Gate mic consumers — when active, no mic frames are dispatched.

        v265.0: Used by speech state manager to suppress self-voice during TTS.
        """
        self._mic_gate_active = active
        logger.debug(f"[AudioBus] Mic gate {'ACTIVE' if active else 'INACTIVE'}")

    @property
    def mic_gate_active(self) -> bool:
        """Check if mic gate is active."""
        return self._mic_gate_active

    # ---- Internal: Mic frame processing ----

    def _fit_to_range(self, frame: np.ndarray) -> np.ndarray:
        """Scale hot frames into range PROPORTIONALLY. Fast attack, slow release.

        THE FAULT THIS REMOVES. The capture device delivers float PCM above
        full scale — measured on this machine at peak 4.19 while the operator
        spoke. CoreAudio is right to do so: 32-bit float PCM has no ceiling,
        and nothing is lost in memory. The loss happens at every consumer that
        quantises, and this pipeline has several — webrtcvad takes int16, the
        WebSocket tap takes int16, speexdsp takes int16 — each of which HARD
        CLIPS what it is given.

        Why proportional and not a soft knee
        ------------------------------------
        The previous version curved excursions through
        ``tanh``, which keeps the peak under 1.0 but is a NONLINEARITY: it
        compresses the loud parts of the waveform against the quiet parts and
        manufactures harmonics that were never spoken. Speech recognition
        reads formant structure, so distorting the relationship between
        harmonics is precisely the wrong tool — it was chosen to avoid hard
        clipping, and it does, but it trades one waveform corruption for a
        subtler one.

        Multiplying the whole frame by a single scalar has no such cost. The
        waveform is IDENTICAL in shape; only its amplitude moves. Every
        harmonic ratio, every formant, every zero crossing survives exactly.
        That is the operation a recogniser wants and the tanh curve is not.

        Attack and release
        ------------------
        Attack is immediate: the gain needed to bring THIS frame under the
        target is applied to THIS frame, so nothing is ever handed downstream
        above the ceiling. There is no lookahead and none is needed — the
        frame is already in hand when its peak is known.

        Release is slow (seconds). Restoring gain quickly would raise the
        noise floor in every pause between words, which is audible as pumping
        and gives the endpointer a moving floor to chase. Asymmetry is the
        whole design: react instantly to loudness, forget it gradually.

        Ramped across the frame
        -----------------------
        Applying a step change in gain at a frame boundary puts a
        discontinuity in the waveform — a click, and a broadband one, which is
        exactly the artefact this exists to avoid. The gain is therefore
        interpolated linearly from the previous frame's value to this one's
        across the frame, so the signal stays continuous.

        Below the ceiling the frame is returned UNTOUCHED and bit-identical.
        NEVER raises."""
        try:
            if frame is None or not frame.size:
                return frame
            out = np.asarray(frame, dtype=np.float32)

            # Non-finite samples would poison the gain state for every frame
            # that follows, so they are neutralised rather than propagated.
            sanitized = False
            if not np.all(np.isfinite(out)):
                out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
                sanitized = True

            peak = float(np.max(np.abs(out)))
            prev = getattr(self, "_agc_gain", 1.0)
            if not np.isfinite(prev) or prev <= 0.0:
                prev = 1.0

            if peak * prev > _AGC_CEILING and peak > 0.0:
                # ATTACK — immediate, sized to this frame.
                #
                # The floor bounds the REMEMBERED gain, not the correction
                # applied here. Flooring the correction let an absurd frame
                # (1e30, a device glitch) leave at 2e28 — the ceiling
                # defeated by the very guard meant to protect recovery. The
                # frame is always scaled by exactly what it needs; the floor
                # only stops one freak transient from parking the session at
                # a gain it cannot climb back from inside the release
                # constant.
                frame_gain = _AGC_TARGET / peak
                target_gain = max(_AGC_MIN_GAIN, frame_gain)
                if peak > 1.0 and getattr(self, "_range_reports", 0) < 3:
                    self._range_reports = getattr(self, "_range_reports", 0) + 1
                    logger.warning(
                        "[AudioBus] input peak %.2f exceeds full scale — "
                        "scaling by %.3f (proportional, waveform preserved); "
                        "the capture device is applying gain",
                        peak, target_gain,
                    )
            elif prev < 1.0:
                # RELEASE — exponential climb back toward unity, per-frame
                # coefficient derived from the frame's own duration so the
                # time constant is in SECONDS and independent of frame size
                # and sample rate.
                frame_gain = target_gain = 1.0    # release path: no attack
                rate = float(getattr(self._config, "internal_rate", 16000) or 16000)
                dt = out.size / max(rate, 1.0)
                coeff = float(np.exp(-dt / _AGC_RELEASE_S))
                target_gain = min(1.0, prev * coeff + 1.0 * (1.0 - coeff))
            elif peak < _AGC_QUIET_PEAK:
                # UPWARD NORMALIZATION.
                #
                # The operator's speech measured peak 0.070 / rms 0.0029 here
                # — 27x quieter than a played probe on the same microphone,
                # and unreadable by whisper from the raw device tap. A
                # governor that only ever attenuates cannot help that; a
                # signal this far down needs to come up.
                #
                # Gated on an ADAPTIVE noise floor rather than a fixed
                # threshold, because "quiet" is a property of the room, not a
                # constant. Boosting the floor would turn a silent room into
                # static and give the endpointer a signal that never stops.
                floor = self._track_noise_floor(peak, out.size)
                gate = max(_AGC_SQUELCH_ABS, floor * _AGC_SQUELCH_RATIO)
                if peak <= gate or peak <= 0.0:
                    # Noise floor, or silence. Left ALONE — not scaled, not
                    # muted: the caller's frame is the honest answer.
                    self._agc_gain = 1.0
                    self._range_peak = max(getattr(self, "_range_peak", 0.0), peak)
                    return out if sanitized else frame
                boost = min(_AGC_MAX_BOOST, _AGC_NOMINAL / peak)
                target_gain = max(1.0, boost)
                frame_gain = target_gain
            else:
                self._track_noise_floor(peak, out.size)
                self._agc_gain = 1.0
                self._range_peak = max(getattr(self, "_range_peak", 0.0), peak)
                # Return the SANITISED array when sanitising happened. The
                # passthrough branch previously returned the caller's frame,
                # which handed NaN/inf straight back out — the one path where
                # "untouched" was the wrong promise.
                return out if sanitized else frame

            self._agc_gain = target_gain
            self._range_peak = max(getattr(self, "_range_peak", 0.0), peak)

            # ASYMMETRY, applied here rather than merely described above.
            #
            # ATTACK is a STEP, not a ramp. Ramping into an attack defeats it:
            # the frame would start at the OLD gain, so its loudest samples —
            # which are why the attack fired — leave the stage still above the
            # ceiling. Caught by the smoke test: a 4.19 frame emerged at 4.14
            # and hard-clipped downstream, i.e. the governor did nothing at
            # exactly the moment it existed for. A step down at the onset of a
            # loud passage is inaudible against the loud passage itself;
            # clipping is not.
            #
            # RELEASE is ramped, because that is where smoothness is worth
            # something and there is no ceiling to breach — gain is rising
            # into headroom the signal is not using.
            if target_gain < prev:
                applied = min(target_gain, frame_gain)
                return (out * np.float32(applied)).astype(np.float32)
            ramp = np.linspace(
                prev, target_gain, out.size, dtype=np.float32, endpoint=True,
            )
            return (out * ramp).astype(np.float32)
        except Exception:  # noqa: BLE001 — audio thread: never propagate
            return frame

    def _track_noise_floor(self, peak: float, n_samples: int) -> float:
        """Follow the room's quiet level. Returns the current floor estimate.

        Asymmetric on purpose, and in the opposite direction to the gain
        release: the floor RISES slowly (a room that gets noisier is learned
        over seconds, so one cough does not raise the gate) and FALLS quickly
        (a room that goes quiet must be believed immediately, or the gate
        stays shut through the operator's next sentence).

        Fed every frame including loud ones — a floor that only sampled quiet
        frames would never learn that the room got louder, and would gate
        speech as though it were still silent. The slow rise is what keeps
        speech from dragging the floor up to meet it."""
        try:
            rate = float(getattr(self._config, "internal_rate", 16000) or 16000)
            dt = max(n_samples, 1) / max(rate, 1.0)
            prev = getattr(self, "_noise_floor", None)
            if prev is None or not np.isfinite(prev) or prev <= 0.0:
                self._noise_floor = max(peak, 1e-6)
                return self._noise_floor
            tau = _AGC_FLOOR_RISE_S if peak > prev else _AGC_FLOOR_FALL_S
            coeff = float(np.exp(-dt / max(tau, 1e-6)))
            self._noise_floor = float(prev * coeff + peak * (1.0 - coeff))
            return self._noise_floor
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            return getattr(self, "_noise_floor", 1e-6)

    def agc_state(self) -> dict:
        """Observability seam — what the governor is currently doing."""
        return {
            "gain": round(float(getattr(self, "_agc_gain", 1.0)), 5),
            "ceiling": _AGC_CEILING,
            "target": _AGC_TARGET,
            "release_s": _AGC_RELEASE_S,
            "peak_seen": round(float(getattr(self, "_range_peak", 0.0)), 4),
            "noise_floor": round(float(getattr(self, "_noise_floor", 0.0) or 0.0), 6),
            "quiet_peak": _AGC_QUIET_PEAK,
            "nominal": _AGC_NOMINAL,
            "max_boost": _AGC_MAX_BOOST,
        }

    def _on_mic_frame(self, raw_frame: np.ndarray) -> None:
        """
        Called from the audio thread with raw mic data at device rate.

        Pipeline: raw mic → downsample → AEC → dispatch to consumers
        """
        if not self._running:
            return

        # v265.0: Mic gate — discard frames when TTS is active
        if self._mic_gate_active:
            return

        try:
            # 1. Downsample from device rate to internal rate
            if self._resampler_down is not None:
                internal_frame = self._resampler_down.process(raw_frame)
            else:
                internal_frame = raw_frame

            # 2. Get AEC reference (last output at device rate, downsampled)
            # v237.0: Use dedicated _resampler_aec_ref to avoid contaminating
            # the mic resampler's internal filter state.
            if self._device is not None and self._aec is not None:
                ref_device = self._device.get_last_output_frame()
                if self._resampler_aec_ref is not None:
                    ref_internal = self._resampler_aec_ref.process(ref_device)
                else:
                    ref_internal = ref_device

                # 3. Apply AEC
                cleaned = self._aec.cancel_echo(internal_frame, ref_internal)
            else:
                cleaned = internal_frame

            # FORENSIC TAP (raw end of the chain). Recorded before any
            # processing so an incident can distinguish "the microphone did
            # not hear a voice" from "we destroyed the voice it heard" —
            # two faults with opposite fixes, indistinguishable from the
            # post-processing audio that diagnosis has had to rely on.
            try:
                from backend.audio.capture_forensics import get_forensics
                get_forensics().note_raw(
                    raw_frame,
                    getattr(self._device, "sample_rate", self._config.sample_rate),
                )
            except (ImportError, AttributeError):
                pass

            # 4. HONOUR THE RANGE CONTRACT before dispatching.
            #
            # Measured live: frames arriving at peak 1.04, 2.36, 3.26, 3.99 —
            # up to FOUR TIMES full scale. Nothing in this codebase multiplies
            # the signal, so macOS is delivering input above +/-1.0 (device
            # input boost). Every downstream consumer assumes normalized
            # audio: faster-whisper contracts on [-1, 1], and handing it 4.0
            # is distortion by the time it arrives — which is precisely the
            # "speech-level amplitude, empty transcript" signature that made
            # this fault so hard to place.
            #
            # Scaled by a DECAYING PEAK rather than clipped or per-frame
            # normalized. Clipping flattens the waveform and destroys exactly
            # the formant structure speech recognition reads; per-frame
            # normalization pumps, making quiet frames as loud as shouted
            # ones and erasing the dynamics an endpointer needs. A decaying
            # peak preserves relative level across an utterance and recovers
            # when the source gets quieter.
            cleaned = self._fit_to_range(cleaned)

            # FORENSIC TAP (model end of the chain) — byte-for-byte what the
            # recogniser is about to be handed.
            try:
                from backend.audio.capture_forensics import get_forensics
                get_forensics().note_processed(cleaned, self._config.internal_rate)
            except (ImportError, AttributeError):
                pass

            # 5. Dispatch to all consumers
            with self._consumer_lock:
                for consumer in self._mic_consumers:
                    try:
                        consumer(cleaned)
                    except Exception:
                        pass  # Never crash the audio thread
            self._mic_frames_delivered += 1
            self._mic_error_count = 0

        except Exception as e:
            # LOUD, not DEBUG. A resampler keyword drift discarded 100% of mic
            # frames here while every status surface still read healthy — the
            # microphone was dead and nothing said so. Total frame loss is not
            # a debug detail; it is the audio path being down.
            #
            # Rate-limited by COUNT rather than time: this runs on the audio
            # thread at 50Hz, so a per-frame log would itself become the fault.
            # The first failure speaks immediately; the rest report as a
            # growing tally on a geometric cadence.
            self._mic_error_count += 1
            n = self._mic_error_count
            if n == 1 or (n & (n - 1)) == 0:      # 1, 2, 4, 8, 16, …
                logger.warning(
                    "[AudioBus] mic frame processing FAILED (%d frame%s "
                    "dropped, 0 delivered to %d consumer%s): %s",
                    n, "" if n == 1 else "s",
                    len(self._mic_consumers),
                    "" if len(self._mic_consumers) == 1 else "s", e,
                )

    # ---- Properties ----

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def device(self) -> Optional[FullDuplexDevice]:
        return self._device

    @property
    def config(self) -> Optional[DeviceConfig]:
        return self._config

    def get_status(self) -> dict:
        """Get current audio bus status."""
        return {
            "running": self._running,
            "device_running": self._device.is_running if self._device else False,
            "mic_consumers": len(self._mic_consumers),
            # Delivery is the only honest health signal for the mic path:
            # "running" was True throughout a total outage.
            "mic_frames_delivered": self._mic_frames_delivered,
            "mic_frame_errors": self._mic_error_count,
            "input_enabled": self._device.input_enabled if self._device else False,
            "sinks": list(self._sinks.keys()),
            "playback_buffered": (
                self._device.playback_buffer.available
                if self._device else 0
            ),
            "aec_type": (
                "speexdsp" if self._aec and self._aec._use_speexdsp
                else "spectral_subtraction" if self._aec else "none"
            ),
            "mic_gate_active": self._mic_gate_active,  # v265.0
        }


# ============================================================================
# Module-level access
# ============================================================================

def get_audio_bus() -> AudioBus:
    """Get the AudioBus singleton."""
    return AudioBus.get_instance()


def get_audio_bus_safe() -> Optional[AudioBus]:
    """Get the AudioBus singleton if it exists and is running."""
    bus = AudioBus.get_instance_safe()
    if bus is not None and bus.is_running:
        return bus
    return bus
