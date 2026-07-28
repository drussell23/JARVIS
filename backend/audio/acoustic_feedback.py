"""The seam between a rejected capture and Karen saying she cannot hear.

Kept separate from :mod:`acoustic_quality` so the metric definitions and the
policy stay independently testable, and separate from ``streaming_stt`` so the
STT never imports a voice path — that direction would close a loop between the
recogniser and the speaker, which is how a system ends up transcribing itself.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

_CONTROLLER: Optional[Any] = None

#: Set by whoever owns the AdaptiveInputManager (the audio plane host). Left
#: as a plain seam rather than an import so this module keeps knowing nothing
#: about CoreAudio, and so the manager stays absent — not merely disabled —
#: in every process that has not deliberately armed it.
_ADAPTIVE_SINK: Optional[Callable[[Any], None]] = None


def set_adaptive_input_sink(sink: Optional[Callable[[Any], None]]) -> None:
    """Register (or clear) the observer that receives every QualitySample."""
    global _ADAPTIVE_SINK
    _ADAPTIVE_SINK = sink


def _notify_adaptive_input(sample: Any) -> None:
    """Hand one measurement to the device manager. NEVER raises: this sits on
    the STT rejection path and must not be able to break recognition."""
    sink = _ADAPTIVE_SINK
    if sink is None:
        return
    try:
        sink(sample)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Acoustic] adaptive sink degraded: %r", exc)


_DEGRADATION_SINK: Optional[Callable[[str, dict], None]] = None


def set_degradation_sink(
    sink: Optional[Callable[[str, dict], None]],
) -> None:
    """Register (or clear) the surface that shows degradation to the operator.

    Injected exactly like `set_adaptive_input_sink` above — the attach server
    is an instance the harness owns, and this module must keep working with
    no cockpit attached at all.
    """
    global _DEGRADATION_SINK
    _DEGRADATION_SINK = sink


def _notify_degradation(kind: str, payload: dict) -> None:
    """Hand one verdict to the cockpit. NEVER raises: this sits on the STT
    rejection path and must not be able to break recognition."""
    sink = _DEGRADATION_SINK
    if sink is None:
        return
    try:
        sink(kind, dict(payload or {}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Acoustic] degradation sink degraded: %r", exc)


def _controller() -> Any:
    """Lazily built, with the emit and speak seams bound to the real bus and
    the real fast path — both looked up late so an audio host without either
    still measures, and only the SPEAKING degrades."""
    global _CONTROLLER
    if _CONTROLLER is not None:
        return _CONTROLLER
    from backend.audio.acoustic_quality import AcousticFeedbackController

    def _emit(kind: str, payload: dict) -> None:
        """Publish one degradation verdict. NEVER raises.

        This used to import `backend.audio.audio_state_ipc.broadcast` — a
        module that does not exist (the real one lives under
        `governance.comms.duplex`) and a function that does not exist there
        either. Both failures landed in `except ImportError: pass`, so the
        gate measured the room correctly, decided correctly, and published
        into nothing. Every degradation event since it shipped was swallowed.

        It publishes to an INJECTED sink instead. The duplex bus would be the
        tidier destination — `SYS_TELEMETRY_DEGRADED` is already a member of
        its closed `EVENT_KINDS` vocabulary — but `publish_event` exists only
        as a method on a server instance with no module-level accessor, and
        inventing one to reach it would be a second guess stacked on the
        first. The sink is how this module already solves exactly this
        problem for the device manager (`set_adaptive_input_sink`), and the
        harness owns both instances.

        The payload travels whole (diagnosis, crest, device): a badge that
        cannot say WHY is a badge nobody acts on.
        """
        _notify_degradation(kind, payload)

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

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not x.size:
            return None
        # DRY: QualitySample.from_audio routes through the forensics ring, so
        # the numbers here and in an incident file cannot disagree. The caller
        # already has peak/rms measured on the same buffer — keep those rather
        # than the ring's, since they are what the rejection log reported.
        sample = QualitySample.from_audio(
            x, sample_rate, no_speech_prob=no_speech_prob, device=device,
        )
        sample = replace(sample, rms=float(rms), peak=float(peak))
        result = _controller().observe(sample)
        _notify_adaptive_input(sample)
        return result
    except (ImportError, AttributeError, TypeError, ValueError):
        logger.debug("[Acoustic] report degraded", exc_info=True)
        return None


def reset() -> None:
    """Test seam."""
    global _CONTROLLER
    _CONTROLLER = None


__all__ = ["report_rejection", "reset", "speak_immediate"]
