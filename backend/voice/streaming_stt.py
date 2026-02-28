"""
Streaming Speech-to-Text Engine (Layer 2)
==========================================

Emits partial transcripts as the user speaks, from AEC-cleaned audio frames.
Wraps faster-whisper for incremental transcription with VAD-based segmentation.

Architecture:
    AudioBus (16kHz AEC-cleaned) ──▶ VAD ──▶ Buffer ──▶ faster-whisper
                                                             │
                                                        StreamingTranscriptEvent
                                                        (partial + final)

The engine registers as a mic consumer on the AudioBus and accumulates frames
until VAD detects end-of-speech, then runs whisper on the accumulated audio.
For partial transcripts, it runs whisper periodically on the accumulated buffer.
"""

import asyncio
import logging
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Deque, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Configuration from environment
_MODEL_SIZE = os.getenv("JARVIS_STT_MODEL", "base")
_PARTIAL_INTERVAL_MS = int(os.getenv("JARVIS_STT_PARTIAL_INTERVAL_MS", "500"))
_MAX_BUFFER_SECONDS = float(os.getenv("JARVIS_STT_MAX_BUFFER_SECONDS", "30.0"))
_VAD_SILENCE_THRESHOLD_MS = int(os.getenv("JARVIS_STT_SILENCE_MS", "600"))
_LANGUAGE = os.getenv("JARVIS_STT_LANGUAGE", "en")


def _startup_asr_admission() -> tuple[bool, str]:
    """
    Shared startup admission contract for heavy ASR model initialization.

    During supervisor startup, ASR is deferred until admission opens to avoid
    contention with critical startup phases.
    """
    if os.getenv("JARVIS_ASR_ADMISSION_FORCE_OPEN", "").lower() in ("1", "true", "yes", "on"):
        return True, "forced_open"
    admission_enabled = os.getenv("JARVIS_ASR_ADMISSION_ENABLED", "true").lower() in (
        "1", "true", "yes", "on"
    )
    if not admission_enabled:
        return True, "admission_disabled"
    admission_open = os.getenv("JARVIS_ASR_ADMISSION_OPEN", "").lower() in (
        "1", "true", "yes", "on"
    )
    if admission_open:
        return True, "admitted"
    startup_complete = os.getenv("JARVIS_STARTUP_COMPLETE", "").lower() == "true"
    if startup_complete:
        return True, "startup_complete"
    reason = os.getenv("JARVIS_ASR_ADMISSION_REASON", "startup_barrier")
    return False, reason


@dataclass
class StreamingTranscriptEvent:
    """A transcript event emitted by the streaming STT engine."""
    text: str
    is_partial: bool      # True = still speaking, False = final
    confidence: float
    timestamp_ms: float
    audio_duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)


class StreamingSTTEngine:
    """
    Streaming speech-to-text using faster-whisper.

    Receives 20ms AEC-cleaned frames from AudioBus at 16kHz.
    Uses webrtcvad to segment speech from silence.
    Emits partial transcripts every ~500ms during speech.
    Emits final transcript after VAD detects end of utterance.
    """

    def __init__(self, sample_rate: int = 16000):
        self._sample_rate = sample_rate
        self._model = None
        self._vad = None

        # Audio accumulation
        self._audio_buffer: Deque[np.ndarray] = deque()
        self._buffer_lock = threading.Lock()
        self._total_frames = 0
        self._max_buffer_frames = int(_MAX_BUFFER_SECONDS * sample_rate)

        # VAD state
        self._speech_active = False
        self._silence_start_ms: Optional[float] = None
        self._speech_start_ms: Optional[float] = None

        # Transcript output queue
        self._transcript_queue: Optional[asyncio.Queue] = None

        # Partial transcript timing
        self._last_partial_time = 0.0

        # Control
        self._running = False
        self._processing_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        """Load the faster-whisper model and initialize VAD."""
        if self._running:
            return
        admitted, admission_reason = _startup_asr_admission()
        if not admitted:
            raise RuntimeError(
                f"ASR admission closed: {admission_reason}. "
                "Retry after startup barrier opens."
            )

        self._loop = asyncio.get_running_loop()
        self._transcript_queue = asyncio.Queue()

        # Load faster-whisper model — cache-aware offline resilience
        try:
            from faster_whisper import WhisperModel

            def _load():
                compute_type = os.getenv("JARVIS_STT_COMPUTE_TYPE", "int8")
                device = os.getenv("JARVIS_STT_DEVICE", "cpu")
                try:
                    return WhisperModel(
                        _MODEL_SIZE,
                        device=device,
                        compute_type=compute_type,
                    )
                except Exception as first_err:
                    # If offline mode blocked a cache-miss download, temporarily
                    # allow online access for this one-time model download.
                    if "outgoing traffic has been disabled" in str(first_err) \
                            or "HF_HUB_OFFLINE" in str(first_err):
                        logger.info(
                            f"[StreamingSTT] Model not cached, temporarily "
                            f"enabling online download for {_MODEL_SIZE}..."
                        )
                        prev_offline = os.environ.get("HF_HUB_OFFLINE")
                        os.environ.pop("HF_HUB_OFFLINE", None)
                        os.environ.pop("TRANSFORMERS_OFFLINE", None)
                        try:
                            model = WhisperModel(
                                _MODEL_SIZE,
                                device=device,
                                compute_type=compute_type,
                            )
                            logger.info("[StreamingSTT] Model downloaded and cached")
                            return model
                        finally:
                            # Restore offline mode after download
                            if prev_offline is not None:
                                os.environ["HF_HUB_OFFLINE"] = prev_offline
                                os.environ["TRANSFORMERS_OFFLINE"] = prev_offline
                    else:
                        raise

            self._model = await self._loop.run_in_executor(None, _load)
            logger.info(
                f"[StreamingSTT] Loaded faster-whisper model: {_MODEL_SIZE}"
            )
        except ImportError:
            logger.error(
                "[StreamingSTT] faster-whisper not installed. "
                "Install with: pip install faster-whisper"
            )
            raise

        # Initialize VAD
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad()
            self._vad.set_mode(int(os.getenv("JARVIS_VAD_MODE", "3")))
            logger.info("[StreamingSTT] VAD initialized (mode 3)")
        except ImportError:
            logger.warning(
                "[StreamingSTT] webrtcvad not available, using energy-based VAD"
            )

        self._running = True
        logger.info("[StreamingSTT] Started")

    async def stop(self) -> None:
        """Stop the engine and release resources."""
        self._running = False
        self._model = None
        self._vad = None

        with self._buffer_lock:
            self._audio_buffer.clear()
            self._total_frames = 0

        if self._transcript_queue is not None:
            # Signal end
            await self._transcript_queue.put(None)

        logger.info("[StreamingSTT] Stopped")

    def on_audio_frame(self, frame: np.ndarray) -> None:
        """
        Called from the audio thread with AEC-cleaned 16kHz frames.
        Performs VAD and accumulates speech frames.
        """
        if not self._running:
            return

        now_ms = time.time() * 1000
        is_speech = self._detect_speech(frame)

        if is_speech:
            if not self._speech_active:
                # Speech just started
                self._speech_active = True
                self._speech_start_ms = now_ms
                self._silence_start_ms = None

            # Accumulate frame
            with self._buffer_lock:
                self._audio_buffer.append(frame.copy())
                self._total_frames += len(frame)

                # Prevent unbounded growth
                while self._total_frames > self._max_buffer_frames:
                    oldest = self._audio_buffer.popleft()
                    self._total_frames -= len(oldest)

            # Emit partial transcript periodically
            if now_ms - self._last_partial_time > _PARTIAL_INTERVAL_MS:
                self._last_partial_time = now_ms
                self._schedule_transcription(is_partial=True)

        else:
            if self._speech_active:
                if self._silence_start_ms is None:
                    self._silence_start_ms = now_ms
                elif now_ms - self._silence_start_ms > _VAD_SILENCE_THRESHOLD_MS:
                    # End of speech detected
                    self._speech_active = False
                    self._schedule_transcription(is_partial=False)

    def _detect_speech(self, frame: np.ndarray) -> bool:
        """Detect speech in a frame using webrtcvad or energy threshold."""
        if self._vad is not None:
            try:
                # webrtcvad needs int16 PCM, 10/20/30ms frames
                frame_i16 = (frame * 32767).clip(-32768, 32767).astype(np.int16)
                frame_bytes = frame_i16.tobytes()

                # webrtcvad requires specific frame sizes
                # 16kHz * 20ms = 320 samples = 640 bytes
                expected_size = self._sample_rate * 20 // 1000
                if len(frame_i16) == expected_size:
                    return self._vad.is_speech(frame_bytes, self._sample_rate)
                elif len(frame_i16) > expected_size:
                    # Use first valid chunk
                    chunk = frame_i16[:expected_size]
                    return self._vad.is_speech(chunk.tobytes(), self._sample_rate)
            except Exception:
                pass

        # Fallback: energy-based VAD
        energy = np.sqrt(np.mean(frame ** 2))
        threshold = float(os.getenv("JARVIS_VAD_ENERGY_THRESHOLD", "0.01"))
        return energy > threshold

    def _schedule_transcription(self, is_partial: bool) -> None:
        """Schedule a transcription job (thread-safe)."""
        if self._loop is None or self._transcript_queue is None:
            return

        with self._buffer_lock:
            if not self._audio_buffer:
                return
            audio = np.concatenate(list(self._audio_buffer))

            if not is_partial:
                # Clear buffer for final transcript
                self._audio_buffer.clear()
                self._total_frames = 0

        # Run transcription in thread pool
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(
                self._run_transcription(audio, is_partial)
            )
        )

    async def _run_transcription(
        self, audio: np.ndarray, is_partial: bool
    ) -> None:
        """Run faster-whisper transcription in executor."""
        if self._model is None or self._transcript_queue is None:
            return

        try:
            loop = asyncio.get_running_loop()

            def _transcribe():
                segments, info = self._model.transcribe(
                    audio,
                    language=_LANGUAGE,
                    beam_size=1 if is_partial else 5,
                    best_of=1 if is_partial else 5,
                    vad_filter=False,  # We handle VAD ourselves
                )
                text_parts = []
                for segment in segments:
                    text_parts.append(segment.text.strip())
                return " ".join(text_parts), getattr(info, "language_probability", 0.9)

            text, confidence = await loop.run_in_executor(None, _transcribe)

            if text:
                event = StreamingTranscriptEvent(
                    text=text,
                    is_partial=is_partial,
                    confidence=confidence,
                    timestamp_ms=time.time() * 1000,
                    audio_duration_ms=(len(audio) / self._sample_rate) * 1000,
                )
                await self._transcript_queue.put(event)

        except Exception as e:
            logger.warning(f"[StreamingSTT] Transcription error: {e}")

    async def get_transcripts(self) -> AsyncIterator[StreamingTranscriptEvent]:
        """
        Async iterator yielding transcript events.

        Yields partial transcripts during speech and final transcripts
        after silence is detected.
        """
        if self._transcript_queue is None:
            return

        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._transcript_queue.get(),
                    timeout=1.0,
                )
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                continue

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_speech_active(self) -> bool:
        return self._speech_active

    def get_status(self) -> dict:
        """Get engine status."""
        return {
            "running": self._running,
            "speech_active": self._speech_active,
            "buffer_frames": self._total_frames,
            "buffer_seconds": self._total_frames / self._sample_rate,
            "model": _MODEL_SIZE,
        }
