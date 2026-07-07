"""backend/core/ouroboros/governance/comms/duplex/tts_playback.py

`ArbiterTTSPlayback` — the production :class:`PlaybackHandle` (Sprint 3).

Bridges the Sprint-1 `VoiceDuplexArbiter` to the REAL audio stack by wrapping
``UnifiedTTSEngine.speak_stream(text, cancel_event=...)`` — the existing
streaming-TTS-with-barge-in-cancellation primitive. Barge-in (`preempt`) simply
sets the cancel event; the engine's streaming loop checks it between chunks and
stops.

Mandate #3 (no redundant driver state): this adapter tracks only the single
``asyncio.Event`` it owns for the current utterance. The audio device, AEC,
resampling, and the playback ring buffer all live in ``backend/audio`` and are
NOT touched here — the arbiter speaks THROUGH the existing engine, it does not
re-implement it.

The live mic → VAD → barge-in → speaker loop is a local interactive
verification (there is no audio device in CI); this module is unit-tested
against a mock engine.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("Ouroboros.Karen.Playback")


class ArbiterTTSPlayback:
    """A :class:`PlaybackHandle` backed by ``UnifiedTTSEngine.speak_stream``.

    ``engine`` is a ``UnifiedTTSEngine`` (or ``None`` for a silent no-op, e.g.
    when the audio stack is unavailable). NEVER raises out of ``play`` /
    ``preempt`` — audio failure must never reach the arbiter or the FSM.
    """

    def __init__(self, engine: object, *, source: str = "karen") -> None:
        self._engine = engine
        self._source = source
        self._active = False
        self._cancel: Optional[asyncio.Event] = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def play(self, text: str) -> None:
        """Speak ``text`` through the streaming TTS engine, cancellable via
        :meth:`preempt`. Returns when playback completes OR is preempted."""
        if not text or self._engine is None:
            return
        speak_stream = getattr(self._engine, "speak_stream", None)
        if not callable(speak_stream):
            return
        self._cancel = asyncio.Event()
        self._active = True
        try:
            await speak_stream(text, cancel_event=self._cancel, source=self._source)
        except asyncio.CancelledError:
            # Arbiter cancelled the play task (barge-in path also cancels) —
            # signal the engine so it stops too, then propagate cleanly.
            self._signal_cancel()
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[Playback] speak_stream failed", exc_info=True)
        finally:
            self._active = False
            self._cancel = None

    def preempt(self) -> None:
        """Barge-in: set the cancel event so ``speak_stream`` stops between
        chunks. Idempotent (a finished / never-started utterance is a no-op).
        NEVER raises."""
        self._signal_cancel()

    def _signal_cancel(self) -> None:
        try:
            if self._cancel is not None:
                self._cancel.set()
        except Exception:  # noqa: BLE001
            logger.debug("[Playback] preempt failed", exc_info=True)


__all__ = ["ArbiterTTSPlayback"]
