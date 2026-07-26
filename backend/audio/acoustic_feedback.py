"""The seam between a rejected capture and Karen saying she cannot hear.

Kept separate from :mod:`acoustic_quality` so the metric definitions and the
policy stay independently testable, and separate from ``streaming_stt`` so the
STT never imports a voice path — that direction would close a loop between the
recogniser and the speaker, which is how a system ends up transcribing itself.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

_CONTROLLER: Optional[Any] = None


def _controller() -> Any:
    """Lazily built, with the emit and speak seams bound to the real bus and
    the real fast path — both looked up late so an audio host without either
    still measures, and only the SPEAKING degrades."""
    global _CONTROLLER
    if _CONTROLLER is not None:
        return _CONTROLLER
    from backend.audio.acoustic_quality import AcousticFeedbackController

    def _emit(kind: str, payload: dict) -> None:
        # DRY: the audio-state UDS server is the existing event bus. No new
        # transport, no second socket.
        try:
            from backend.audio.audio_state_ipc import broadcast
            broadcast(kind, payload)
        except (ImportError, AttributeError, OSError):
            pass

    def _speak(line: str) -> None:
        # Routed through the SAME zero-cost path the phatic acknowledgements
        # use: this is Karen reporting her own limitation, not a model turn,
        # and it must never cost a token or wait on a provider.
        speak_immediate(line)

    _CONTROLLER = AcousticFeedbackController(emit=_emit, speak=_speak)
    return _CONTROLLER


def _note_ticket_outcome(task: Any) -> None:
    """Retrieve a speech ticket's result so a failure is LOGGED, not lost.

    An un-retrieved task exception surfaces only as an interpreter warning on
    stderr, which in a daemon means nowhere. NEVER raises."""
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        return
    if exc is not None:
        logger.warning("[Acoustic] speech ticket failed: %r", exc)


def speak_immediate(line: str) -> bool:
    """Say *line* now, ahead of anything queued. Returns True if it was spoken.

    This is Karen admitting she cannot hear — it is worth interrupting for, and
    it must never cost a token or wait on a provider. So it takes a
    ``SpeechRole.PRIMARY`` ticket on the EXISTING turnstile rather than opening
    its own playback path: a second player would race the first, and two voices
    over one speaker is the collision the scheduler was built to prevent.

    THE OMISSION THIS FIXES. ``acoustic_feedback`` referenced this function
    before it existed, so every degradation logged ``(unspoken)`` — the
    measurement half of the loop worked perfectly and the delivery half had
    never once executed. A dependency referenced but not defined is the
    wired-but-inert trap, and it deserves to be named rather than quietly
    patched.

    Callable from ANY context. With a running loop the ticket is scheduled on
    it; without one the utterance runs inline on this thread. The synthesis
    itself is ``macos_voice``'s — reused, not reimplemented — and it already
    takes ``playback_gate_sync`` around its own ``afplay``, so the microphone
    is closed for exactly as long as sound is leaving the speakers.

    NEVER raises: this sits on the STT rejection path."""
    text = str(line or "").strip()
    if not text:
        return False

    def _render() -> None:
        # macos_voice owns synthesis AND the playback gate. Calling its
        # existing entry point is the whole implementation; anything more
        # would be a second audio stack.
        #
        # `say_and_wait`, not `say`: this runs inside the speech scheduler's
        # ticket, and the scheduler holds the floor for exactly as long as
        # this call takes. `say` only enqueues, so it would return instantly,
        # release the floor, and let the next speaker talk over this one.
        #
        # It was `.speak()`, which MacOSVoice has never defined — the class
        # exposes `say` / `say_and_wait`. So the delivery half STILL never
        # ran: the AttributeError was raised inside an executor called from a
        # fire-and-forget `create_task`, which lands as "Task exception was
        # never retrieved" rather than reaching the guard below. The same
        # wired-but-inert trap this function's docstring was written to
        # close, one layer further down, hidden by the async boundary.
        from backend.voice.macos_voice import MacOSVoice
        MacOSVoice().say_and_wait(text)

    async def _ticket() -> None:
        from backend.audio.speech_scheduler import SpeechRole, get_scheduler
        loop = asyncio.get_running_loop()
        await get_scheduler().speak(
            lambda: loop.run_in_executor(None, _render),
            agent="acoustic", role=SpeechRole.PRIMARY, text=text,
        )

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # Scheduled, never awaited inline: this is called from the STT
            # rejection path, and blocking it would stall recognition to
            # announce that recognition is failing.
            task = loop.create_task(_ticket())
            # ...but fire-and-forget must not mean fail-and-never-know. The
            # guard below cannot see inside a task, which is exactly how a
            # call to a non-existent method survived two releases: the
            # delivery half raised on every single invocation and the only
            # trace was an interpreter-level "Task exception was never
            # retrieved" on stderr. Retrieving it is the difference between a
            # silent dead feature and a log line naming it.
            task.add_done_callback(_note_ticket_outcome)
        else:
            _render()
        logger.info("[Acoustic] speaking: %s", text)
        return True
    except (ImportError, AttributeError, RuntimeError, OSError) as exc:
        logger.warning("[Acoustic] could not speak %r: %r", text, exc)
        return False


def report_rejection(audio: Any, sample_rate: int, peak: float, rms: float,
                     no_speech_prob: float = 0.0, device: str = "") -> Optional[dict]:
    """Turn one empty transcript into a measurement. NEVER raises."""
    try:
        from backend.audio.acoustic_quality import QualitySample
        from backend.audio.capture_forensics import _Ring

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not x.size:
            return None
        # DRY: the ring's stats are the forensics' own formulas, so the numbers
        # here and in an incident file cannot disagree.
        ring = _Ring(sample_rate, max(1.0, x.size / max(sample_rate, 1)))
        ring.push(x)
        st = ring.stats()
        sample = QualitySample(
            modulation=float(st.get("syllabic_modulation_2_8hz", 0.0)),
            crest_db=float(st.get("crest_db", 0.0)),
            rms=float(rms), peak=float(peak),
            no_speech_prob=float(no_speech_prob), device=str(device),
        )
        return _controller().observe(sample)
    except (ImportError, AttributeError, TypeError, ValueError):
        logger.debug("[Acoustic] report degraded", exc_info=True)
        return None


def reset() -> None:
    """Test seam."""
    global _CONTROLLER
    _CONTROLLER = None


__all__ = ["report_rejection", "reset", "speak_immediate"]
