"""
Barge-In Controller (Layer 4)
==============================

When the user speaks over JARVIS, cancel TTS and switch to listening.
No cooldown needed in conversation mode — AEC handles echo suppression.

Architecture:
    VAD (AEC-cleaned) ──▶ BargeInController ──▶ cancel_event.set()
                                                     │
                                              AudioBus.flush_playback()
                                              SpeechState.stop_speaking()

The controller monitors the AEC-cleaned VAD output. If speech is detected
while JARVIS is playing TTS, it triggers a barge-in:
    1. Sets the cancel event (stops TTS streaming)
    2. Flushes the playback buffer (silence within one frame)
    3. Notifies the speech state manager
"""

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum consecutive speech frames before triggering barge-in
# Prevents single-frame noise from interrupting
#: Sustained speech required to call it an interruption, in MILLISECONDS
#: rather than a frame count — a count silently changes meaning if the frame
#: duration ever does, and it hid how short this really was.
#:
#: The old value was 3 frames = SIXTY MILLISECONDS. Karen's own voice leaking
#: into the microphone for 60ms is not a possibility, it is a certainty, and
#: it cancelled her reply within 300ms every single time.
_MIN_SPEECH_MS = int(os.getenv("JARVIS_BARGEIN_MIN_SPEECH_MS", "120"))

#: The same requirement WHILE JARVIS IS AUDIBLE, where every frame is
#: echo-suspect. A person who genuinely means to interrupt keeps talking; an
#: echo burst is brief and stops when she does. Asymmetry is the point — it
#: costs a deliberate interrupter a fraction of a second and costs an echo
#: everything.
_MIN_SPEECH_MS_WHILE_SPEAKING = int(
    os.getenv("JARVIS_BARGEIN_MIN_SPEECH_MS_SPEAKING", "600")
)

#: Frame duration the VAD consumer delivers, so the thresholds above stay
#: expressed in time no matter how the audio path is framed.
_FRAME_MS = max(1, int(os.getenv("JARVIS_AUDIO_FRAME_MS", "20")))

# Legacy name retained: some call sites and tests reference it.
_MIN_SPEECH_FRAMES = max(1, _MIN_SPEECH_MS // _FRAME_MS)

# Cooldown after barge-in before allowing another (ms)
_BARGEIN_COOLDOWN_MS = int(os.getenv("JARVIS_BARGEIN_COOLDOWN_MS", "500"))


class BargeInController:
    """
    Monitors AEC-cleaned VAD output and interrupts TTS when the user speaks.

    In conversation mode (with AEC), no post-speech cooldown is needed.
    The AEC removes the speaker output from the mic, so any speech detected
    on the cleaned signal is genuinely the user.
    """

    def __init__(self):
        self._cancel_event = asyncio.Event()
        self._speech_frame_count = 0
        self._last_barge_in_ms: float = 0.0
        self._enabled = True

        # Stats
        self._total_barge_ins = 0
        self._suppressed_barge_ins = 0

        # References set during wiring
        self._audio_bus = None
        self._speech_state = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_audio_bus(self, audio_bus) -> None:
        """Set the AudioBus reference for flush_playback."""
        self._audio_bus = audio_bus

    def set_speech_state(self, speech_state) -> None:
        """Set the UnifiedSpeechStateManager reference."""
        self._speech_state = speech_state

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop for scheduling async operations."""
        self._loop = loop

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def on_vad_speech_detected(self, is_speech: bool) -> None:
        """
        Called from the audio thread with VAD results on AEC-cleaned signal.

        If speech is detected while JARVIS is speaking, trigger barge-in.
        """
        if not self._enabled:
            return

        if is_speech:
            self._speech_frame_count += 1

            # ADAPTIVE THRESHOLD. Barge-in only matters while JARVIS is
            # speaking — which is exactly when the microphone is hearing HIM.
            # So the bar is raised for precisely the window in which every
            # frame is echo-suspect, instead of applying one hair-trigger
            # count to both cases.
            speaking = self._is_jarvis_speaking()
            required_ms = (
                _MIN_SPEECH_MS_WHILE_SPEAKING if speaking else _MIN_SPEECH_MS
            )
            required = max(1, required_ms // _FRAME_MS)

            if self._speech_frame_count >= required and speaking:
                logger.debug(
                    "[BargeIn] sustained %dms of speech while speaking "
                    "(threshold %dms) — treating as a real interruption",
                    self._speech_frame_count * _FRAME_MS, required_ms,
                )
                self._trigger_barge_in()
        else:
            self._speech_frame_count = 0

    def _is_jarvis_speaking(self) -> bool:
        """Check if JARVIS is currently outputting audio."""
        # Check AudioBus playback buffer
        if self._audio_bus is not None:
            try:
                buf = self._audio_bus.device
                if buf is not None and buf.playback_buffer.available > 0:
                    return True
            except Exception:
                pass

        # Check speech state manager
        if self._speech_state is not None:
            try:
                return self._speech_state.is_speaking
            except Exception:
                pass

        return False

    def _trigger_barge_in(self) -> None:
        """Execute barge-in: cancel TTS, flush audio, notify state."""
        now_ms = time.time() * 1000

        # Cooldown check
        if now_ms - self._last_barge_in_ms < _BARGEIN_COOLDOWN_MS:
            self._suppressed_barge_ins += 1
            return

        self._last_barge_in_ms = now_ms
        self._total_barge_ins += 1
        self._speech_frame_count = 0

        logger.info(
            f"[BargeIn] User interrupted JARVIS "
            f"(total: {self._total_barge_ins})"
        )

        # 1. Set cancel event (stops streaming TTS)
        self._cancel_event.set()

        # 2. Flush playback buffer
        if self._audio_bus is not None:
            try:
                flushed = self._audio_bus.flush_playback()
                logger.debug(f"[BargeIn] Flushed {flushed} frames")
            except Exception as e:
                logger.debug(f"[BargeIn] Flush error: {e}")

        # 3. Notify speech state (schedule async on event loop)
        if self._speech_state is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(
                        self._speech_state.stop_speaking()
                    )
                )
            except Exception:
                pass

    def get_cancel_event(self) -> asyncio.Event:
        """
        Get the cancellation event. TTS streaming should check this
        and stop if set.
        """
        return self._cancel_event

    def reset(self) -> None:
        """Reset after barge-in has been handled."""
        self._cancel_event.clear()
        self._speech_frame_count = 0

    def get_status(self) -> dict:
        """Get controller status."""
        return {
            "enabled": self._enabled,
            "total_barge_ins": self._total_barge_ins,
            "suppressed_barge_ins": self._suppressed_barge_ins,
            "cancel_event_set": self._cancel_event.is_set(),
            "speech_frame_count": self._speech_frame_count,
        }
