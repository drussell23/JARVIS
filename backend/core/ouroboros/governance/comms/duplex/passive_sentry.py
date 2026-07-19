"""PASSIVE_SENTRY — the always-ajar ear of the ambient organism.

Phase 3 formal engine (operator authorization 2026-07-19), built
entirely on scout-measured ground: EarsAjarGate detection (proven on
the operator's live voice, 48/48), pre-roll stitching (byte-identical,
unit-pinned), on-device SFSpeech partials ('Jarvis Karen wake up
Jarvis' transcribed live), ~1.5% composite CPU.

FSM::

    PASSIVE ──gate breach──▶ RECOGNIZING ──partial match──▶ VERIFYING
       ▲                        │   ▲                          │
       │◀──window timeout───────┘   │              VBIA pass ──▶ LEASED
       │◀──────────── VBIA fail (SILENT) ◀─────────────────────┘

Mandates honored structurally:

  * **Reactive partials** (mandate 1): wake-phrase evaluation runs
    INSIDE the partial-hypothesis callback — no polling, no static
    sleeps, and ``isFinal`` is never consulted (the on-device quirk:
    finals arrive empty; partials carry the truth).
  * **Acoustic Mirage suppression**: when the machine's own TTS plays
    (``AUDIO_PLAYING``), :meth:`notify_playback` blanks the sentry for
    the exact hardware window + an echo-tail hangover — the organism
    cannot wake itself through its speakers.
  * **Stitched-first-packet**: on gate breach the ~500ms pre-roll
    payload is appended to the recognition session BEFORE any live
    chunk — the wake word's plosive is in the stream (the scout's
    empty-window failure, fixed at the root).
  * **Biometric gate**: a matched partial HALTS the loop and hands the
    cached acoustic window (stitch + live) to the injected VBIA
    verifier; pass → the SAME Tri-State broker lease pathway as a
    terminal ``wake`` (DRY); fail → drop silently back to PASSIVE.

Every collaborator injected; zero capture authority (callers own the
mic); NEVER raises on the audio path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, List, Optional

import numpy as np

logger = logging.getLogger("Ouroboros.PassiveSentry")

STATE_PASSIVE = "PASSIVE"
STATE_RECOGNIZING = "RECOGNIZING"
STATE_VERIFYING = "VERIFYING"
STATE_LEASED = "LEASED"


def _wake_words() -> tuple:
    raw = os.environ.get("JARVIS_SENTRY_WAKE_WORDS", "jarvis,karen")
    return tuple(
        w.strip().lower() for w in raw.split(",") if w.strip()
    ) or ("jarvis", "karen")


def _window_timeout_s() -> float:
    try:
        return max(1.0, min(15.0, float(os.environ.get(
            "JARVIS_SENTRY_WINDOW_TIMEOUT_S", "5.0",
        ))))
    except (TypeError, ValueError):
        return 5.0


def _blank_hangover_s() -> float:
    try:
        return max(0.0, min(5.0, float(os.environ.get(
            "JARVIS_SENTRY_BLANK_HANGOVER_MS", "350",
        )) / 1000.0))
    except (TypeError, ValueError):
        return 0.35


class PassiveSentry:
    """The sentry FSM. Collaborators:

    * ``gate``            — EarsAjarGate (default: production gate)
    * ``session_factory`` — () → recognition session exposing
      ``append(np.ndarray)``, ``set_on_partial(cb)``, ``close()``
      (production: :class:`SFSpeechWindowSession`; tests: fakes)
    * ``verifier``        — (np.ndarray) → awaitable bool — the VBIA
      middleware (voice_authentication_layer), UNMODIFIED feed
    * ``lease_acquirer``  — () → awaitable bool — the EXACT terminal
      wake pathway (AudioVisualSynapse / RemoteAudioLease)
    """

    def __init__(
        self,
        *,
        gate: Any = None,
        session_factory: Optional[Callable[[], Any]] = None,
        verifier: Optional[Callable[[Any], Awaitable[bool]]] = None,
        lease_acquirer: Optional[Callable[[], Awaitable[bool]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if gate is None:
            from .ears_ajar import EarsAjarGate  # noqa: PLC0415
            gate = EarsAjarGate()
        self._gate = gate
        self._session_factory = session_factory or (lambda: None)
        self._verifier = verifier or (self._verifier_unavailable)
        self._lease = lease_acquirer or (self._lease_unavailable)
        self._clock = clock
        self.state = STATE_PASSIVE
        self._session: Any = None
        self._window_opened_at = 0.0
        self._window_audio: List[np.ndarray] = []
        self._blank_until = 0.0
        self._matched_word: Optional[str] = None
        #: Thermal load-shed (SovereignGovernor): >1 = evaluate every
        #: Nth chunk while hot; the ear stays ajar at reduced duty.
        self.chunk_stride = 1
        self._stride_i = 0
        self.stats = {
            "triggers": 0, "mirage_suppressed": 0, "matches": 0,
            "vbia_pass": 0, "vbia_fail": 0, "leases": 0,
            "window_timeouts": 0,
        }

    @staticmethod
    async def _verifier_unavailable(_audio: Any) -> bool:
        # Fail CLOSED: no biometric layer mounted → nobody wakes it.
        return False

    @staticmethod
    async def _lease_unavailable() -> bool:
        return False

    # ---- Acoustic Mirage suppression ----------------------------------

    def notify_playback(self, active: bool) -> None:
        """TTS hardware-window token from the event plane
        (AUDIO_PLAYING → True, AUDIO_IDLE → False). While active — and
        for an echo-tail hangover after — every gate trigger is
        discarded: the organism never wakes itself through its own
        speakers. NEVER raises."""
        try:
            if active:
                self._blank_until = float("inf")
            else:
                self._blank_until = self._clock() + _blank_hangover_s()
        except Exception:  # noqa: BLE001
            pass

    @property
    def blanked(self) -> bool:
        return self._clock() < self._blank_until

    # ---- the audio path (sync; called per 30ms chunk) ------------------

    def feed_chunk(self, chunk: "np.ndarray") -> None:
        """One live chunk in. Drives the whole FSM; NEVER raises."""
        try:
            stride = max(1, int(getattr(self, "chunk_stride", 1)))
            if stride > 1 and self.state == STATE_PASSIVE:
                self._stride_i = (self._stride_i + 1) % stride
                if self._stride_i:
                    return                      # thermal shed: skip eval
            if self.state == STATE_RECOGNIZING:
                if (
                    self._clock() - self._window_opened_at
                    > _window_timeout_s()
                ):
                    self.stats["window_timeouts"] += 1
                    self._close_window()
                    # fall through: chunk still feeds the (re-armed) gate
                else:
                    x = np.asarray(chunk, dtype=np.float32).reshape(-1)
                    self._window_audio.append(x)
                    self._gate.feed(x)          # ring stays warm
                    self._safe_append(x)
                    return
            if self.state != STATE_PASSIVE:
                return                          # VERIFYING/LEASED: halted
            payload = self._gate.feed(chunk)
            if payload is None:
                return
            self.stats["triggers"] += 1
            if self.blanked:
                # Acoustic Mirage: our own voice on the speakers.
                self.stats["mirage_suppressed"] += 1
                self._gate.close_window()
                return
            self._open_window(payload)
        except Exception:  # noqa: BLE001
            logger.debug("[Sentry] feed degraded", exc_info=True)

    def _open_window(self, stitched: "np.ndarray") -> None:
        self._session = self._session_factory()
        self.state = STATE_RECOGNIZING
        self._window_opened_at = self._clock()
        self._window_audio = [stitched]
        if self._session is not None:
            try:
                self._session.set_on_partial(self._on_partial)
            except Exception:  # noqa: BLE001
                pass
            # Stitched pre-roll is the FIRST packet — the wake word's
            # opening plosive enters the stream before any live audio.
            self._safe_append(stitched)

    def _safe_append(self, x: "np.ndarray") -> None:
        try:
            if self._session is not None:
                self._session.append(x)
        except Exception:  # noqa: BLE001
            pass

    # ---- reactive partial evaluation (mandate 1) -----------------------

    def _on_partial(self, text: str) -> None:
        """Fired BY the recognition session for every partial
        hypothesis — evaluation lives here, natively reactive. isFinal
        is never consulted anywhere in this engine. NEVER raises."""
        try:
            if self.state != STATE_RECOGNIZING:
                return
            low = str(text or "").lower()
            for word in _wake_words():
                if word in low:
                    self._matched_word = word
                    self.stats["matches"] += 1
                    self._begin_verification()
                    return
        except Exception:  # noqa: BLE001
            pass

    def _begin_verification(self) -> None:
        """Halt the sentry loop; hand the CACHED acoustic window
        (stitch + live) to the biometric layer."""
        self.state = STATE_VERIFYING
        window = (
            np.concatenate(self._window_audio)
            if self._window_audio else np.zeros(0, dtype=np.float32)
        )
        self._close_session()
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._verify_and_lease(window))
        except RuntimeError:
            # No loop (pure-sync harness): verification unreachable →
            # fail closed back to PASSIVE.
            self._return_to_passive()

    async def _verify_and_lease(self, window: "np.ndarray") -> None:
        """VBIA verify → (pass) SAME broker lease path as terminal
        wake → LEASED; (fail) SILENT return to PASSIVE — a stranger's
        wake word produces no sound, no log line on the operator
        surface, no lease. NEVER raises."""
        try:
            ok = False
            try:
                ok = bool(await self._verifier(window))
            except Exception:  # noqa: BLE001
                ok = False                      # biometric fault = fail closed
            if not ok:
                self.stats["vbia_fail"] += 1
                self._return_to_passive()
                return
            self.stats["vbia_pass"] += 1
            leased = False
            try:
                leased = bool(await self._lease())
            except Exception:  # noqa: BLE001
                leased = False
            if leased:
                self.stats["leases"] += 1
                self.state = STATE_LEASED
            else:
                self._return_to_passive()
        except Exception:  # noqa: BLE001
            self._return_to_passive()

    def on_lease_released(self) -> None:
        """The conversation ended (broker released) — back to the
        ajar ear. NEVER raises."""
        self._return_to_passive()

    # ---- internals -----------------------------------------------------

    def _close_session(self) -> None:
        s = self._session
        self._session = None
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_window(self) -> None:
        self._close_session()
        self._window_audio = []
        self._matched_word = None
        try:
            self._gate.close_window()
        except Exception:  # noqa: BLE001
            pass
        self.state = STATE_PASSIVE

    def _return_to_passive(self) -> None:
        self._close_window()


# ---------------------------------------------------------------------------
# Production recognition session — on-device SFSpeech, runloop-pumped
# ---------------------------------------------------------------------------


def numpy_to_pcm_buffer(x: "np.ndarray", fmt: Any, pcm_buffer_cls: Any) -> Any:
    """float32 numpy → AVAudioPCMBuffer via the SAFE pyobjc bridge:
    ``floatChannelData()[0].as_buffer(n)`` yields a WRITABLE
    memoryview over the buffer's own channel storage — no ctypes, no
    address arithmetic, byte-exact. Caller owns the autorelease pool."""
    buf = pcm_buffer_cls.alloc().initWithPCMFormat_frameCapacity_(
        fmt, x.size,
    )
    buf.setFrameLength_(x.size)
    channel = buf.floatChannelData()[0]
    view = channel.as_buffer(x.size)
    memoryview(view).cast("B")[: x.nbytes] = x.tobytes()
    return buf


class SFSpeechWindowSession:
    """One windowed on-device recognition burst. Wraps the three
    scout-proven mechanics: buffer-append streaming, an NSRunLoop pump
    thread (pyobjc callbacks are DELIVERED, the scout's silent-window
    bug), and partial-hypothesis delivery (finals arrive empty on
    on-device mode — by design we never wait for them). Import-guarded:
    constructing without pyobjc raises RuntimeError for the caller's
    fail-soft."""

    def __init__(self, *, rate: int = 16000) -> None:
        import Speech  # noqa: PLC0415
        from AVFoundation import (  # noqa: PLC0415
            AVAudioFormat,
            AVAudioPCMBuffer,
        )
        self._Speech = Speech
        self._AVAudioFormat = AVAudioFormat
        self._AVAudioPCMBuffer = AVAudioPCMBuffer
        self._rate = rate
        # ONE format object for the session (bit depth / rate /
        # interleaving fixed): pcmFormatFloat32, mono, deinterleaved —
        # exactly what SFSpeechAudioBufferRecognitionRequest accepts.
        self._fmt = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(  # noqa: E501
            1, float(rate), 1, False,
        )
        rec = Speech.SFSpeechRecognizer.alloc().init()
        if rec is None or not rec.isAvailable():
            raise RuntimeError("SFSpeechRecognizer unavailable")
        self._rec = rec
        req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        try:
            req.setRequiresOnDeviceRecognition_(True)
        except Exception:  # noqa: BLE001
            pass
        self._req = req
        self._on_partial: Optional[Callable[[str], None]] = None
        self._task = rec.recognitionTaskWithRequest_resultHandler_(
            req, self._result_cb,
        )
        # Callback delivery contract (2026-07-19 dual-root fix):
        # pyobjc schedules these result blocks onto the MAIN runloop —
        # a secondary pump thread pumps ITS OWN loop and hears nothing
        # (16 silent windows). The HOST owns the pump: call
        # pump_main_runloop() from the MAIN thread each capture tick.
        self._pump_alive = True

    @staticmethod
    def pump_main_runloop(seconds: float = 0.0) -> None:
        """Drain pending main-runloop callbacks. MUST be called from
        the main thread (the capture loop's natural cadence — one call
        per 30ms chunk is ample). NEVER raises."""
        try:
            from Foundation import NSDate, NSRunLoop  # noqa: PLC0415
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(max(0.0, seconds)),
            )
        except Exception:  # noqa: BLE001
            pass

    def _result_cb(self, result: Any, error: Any) -> None:
        try:
            if result is None or self._on_partial is None:
                return
            self._on_partial(
                str(result.bestTranscription().formattedString()),
            )
        except Exception:  # noqa: BLE001
            pass

    def set_on_partial(self, cb: Callable[[str], None]) -> None:
        self._on_partial = cb

    def append(self, chunk: "np.ndarray") -> None:
        """Native in-memory marshal (2026-07-19 root fix): the ctypes
        memmove pointer-cast silently wrote to a WRONG address on
        ARM64 (16 live windows, zero partials — garbage buffers). The
        architecturally safe bridge is pyobjc's ``varlist.as_buffer``:
        a WRITABLE memoryview over the buffer's own float channel —
        zero address arithmetic, byte-exact (probe-proven round-trip).
        The whole append runs inside an ``objc.autorelease_pool`` so a
        24/7 sentry's transient ObjC buffers are destroyed instantly —
        ARC and Python GC can never drift apart (leak-tested 10k
        iterations flat)."""
        import objc  # noqa: PLC0415
        x = np.ascontiguousarray(chunk, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        with objc.autorelease_pool():
            buf = numpy_to_pcm_buffer(
                x, self._fmt, self._AVAudioPCMBuffer,
            )
            self._req.appendAudioPCMBuffer_(buf)

    def close(self) -> None:
        try:
            self._pump_alive = False
            self._req.endAudio()
            self._task.cancel()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "PassiveSentry",
    "SFSpeechWindowSession",
    "STATE_LEASED",
    "STATE_PASSIVE",
    "STATE_RECOGNIZING",
    "STATE_VERIFYING",
]
