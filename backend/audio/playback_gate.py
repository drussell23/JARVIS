"""Close the microphone for exactly as long as sound is leaving the speakers.

Why a gate at all
-----------------
Karen's voice reaches the microphone, the barge-in detector reads it as the
operator cutting in, and it cancels her mid-sentence. Observed live::

    [TTS] Synthesized 29 chars (latency: 5142ms)
    [BargeIn] User interrupted JARVIS (total: 1)

She interrupted herself. AEC cannot cover it: playback leaves through
``afplay`` in a separate process precisely so it is GIL-free, so AudioBus
holds no reference signal to subtract.

Why JUST-IN-TIME
----------------
The obvious placement — gate around the whole speak call — deafens the
microphone through the entire GENERATION phase. That synthesis measured
5142ms for one sentence, and the LLM call precedes it. An assistant that
cannot be interrupted while it is merely thinking is a worse interface than
one that occasionally hears itself, because the operator's only recourse is
to wait out a reply they already know is wrong.

So the gate is bound to PLAYBACK. It closes at the instant sound starts
leaving the speakers and opens the moment it stops. Everything before that —
the LLM, the network, the synthesis — happens with the microphone live and
barge-in armed.

Contract
--------
* ``async with playback_gate(text):`` for async playback sites.
* ``with playback_gate_sync(text):`` for the ``afplay`` subprocess site,
  which is synchronous and lives on a worker thread.

Both release in a ``finally``: a gate left closed by a crash is a permanently
DEAF microphone, strictly worse than the bug it prevents.

Exception discipline
--------------------
Narrow, deliberately. A broad ``except Exception`` in the previous version of
this logic swallowed an ``AttributeError`` from a mis-named enum member and
the gate silently never engaged — a reference error wearing the costume of a
graceful degradation. Import/attribute/type errors are DEFECTS and propagate
during development; only the runtime unavailability of the speech-state
manager is tolerated, and only where the alternative is refusing to speak.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import AsyncIterator, Iterator

logger = logging.getLogger(__name__)


def gate_enabled() -> bool:
    """Master switch. OFF restores the pre-gate behaviour exactly, which is
    the only honest way to compare the two."""
    return os.getenv("JARVIS_PLAYBACK_GATE_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


#: Re-entrancy depth. Both the async and sync gates share it, because the
#: legacy path can nest one inside the other and the INNER exit must not
#: reopen the microphone while the outer body is still playing.
_DEPTH = 0
_DEPTH_LOCK = threading.Lock()


def _set_bus_gate(active: bool) -> bool:
    """Drive AudioBus's mic gate directly. True iff the bus accepted it.

    Deliberately NOT routed through UnifiedSpeechStateManager: that manager
    owns cooldowns, echo-similarity windows and cross-component arbitration,
    and this needs one boolean applied at a precise millisecond. Borrowing the
    heavier surface would mean inheriting policy that has nothing to do with
    "is sound leaving the speakers right now".

    Tolerates a missing bus (no audio mounted) but NOT a broken one: an
    AttributeError here means the gate contract changed and must be loud.
    """
    try:
        from backend.audio.audio_bus import AudioBus
    except ImportError:
        return False
    bus = AudioBus.get_instance_safe()
    if bus is None:
        return False
    bus.set_mic_gate(active)
    return True


def _enter(text: str = "") -> bool:
    global _DEPTH
    if not gate_enabled():
        return False
    with _DEPTH_LOCK:
        first = _DEPTH == 0
        _DEPTH += 1
    if not first:
        return True                       # already closed by an outer gate
    try:
        ok = _set_bus_gate(True)
    except (RuntimeError, ValueError, OSError) as exc:
        # Runtime unavailability only. A NameError/AttributeError from a
        # contract change is NOT caught here and will surface.
        logger.debug("[PlaybackGate] engage unavailable: %r", exc)
        with _DEPTH_LOCK:
            _DEPTH -= 1
        return False
    if not ok:
        # No bus mounted: nothing was closed, so nothing must be counted.
        # Leaving the depth incremented here leaked one level per utterance
        # and the counter never returned to zero — after which a REAL gate
        # would nest instead of engaging, and the mic would stay open forever.
        with _DEPTH_LOCK:
            _DEPTH -= 1
        return False
    logger.debug("[PlaybackGate] mic CLOSED for playback (%d chars)", len(text))
    return True


def _exit() -> None:
    global _DEPTH
    with _DEPTH_LOCK:
        _DEPTH = max(0, _DEPTH - 1)
        last = _DEPTH == 0
    if not last:
        return
    try:
        _set_bus_gate(False)
        logger.debug("[PlaybackGate] mic OPEN")
    except (RuntimeError, ValueError, OSError) as exc:
        # Failing to REOPEN is the dangerous direction — it leaves the
        # operator unheard — so it is reported at WARNING, not swallowed.
        logger.warning("[PlaybackGate] failed to reopen the mic: %r", exc)


async def _await_playback_drain(timeout_s: float = 30.0) -> None:
    """Wait until the speaker ring buffer is actually empty. NEVER raises.

    ``AudioBus.play_stream`` is NOT blocking — it writes into the device's
    PlaybackRingBuffer and returns, and the output callback drains it over
    the following seconds. Gating around that CALL therefore gated nothing:

        13:18:17,324  mic CLOSED for playback (16 chars)
        13:18:17,331  mic OPEN                          <- 7ms later
        13:18:17,631  [BargeIn] User interrupted JARVIS

    Seven milliseconds of protection for several seconds of speech. The mic
    reopened while Karen was still talking, heard her, and cut her off.

    So the gate is held until the audio has actually been CONSUMED. Bounded,
    because a wedged device must not deafen the operator forever."""
    import asyncio as _aio

    try:
        from backend.audio.audio_bus import AudioBus
    except ImportError:
        return
    bus = AudioBus.get_instance_safe()
    device = getattr(bus, "_device", None) if bus is not None else None
    buf = getattr(device, "playback_buffer", None)
    if buf is None:
        return
    deadline = _aio.get_running_loop().time() + max(0.5, timeout_s)
    # Two consecutive empty reads before releasing: the buffer momentarily
    # reads empty between a write and the callback picking it up, and
    # releasing in that gap reopens the mic mid-utterance.
    empty_streak = 0
    while _aio.get_running_loop().time() < deadline:
        try:
            pending = int(getattr(buf, "available", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return
        if pending <= 0:
            empty_streak += 1
            if empty_streak >= 2:
                return
        else:
            empty_streak = 0
        await _aio.sleep(0.02)
    logger.warning(
        "[PlaybackGate] playback did not drain within %.1fs — reopening the "
        "mic rather than leaving the operator unheard", timeout_s,
    )


@contextlib.asynccontextmanager
async def playback_gate(
    text: str = "", *, await_drain: bool = True,
) -> AsyncIterator[bool]:
    """Close the mic for the body — and, for buffered sinks, until it drains.

    ``await_drain`` exists because the two playback shapes differ: a
    subprocess (afplay) is finished when the call returns, while a ring
    buffer is only finished when it empties. Holding the gate on the call
    alone protects the first and nothing at all for the second."""
    engaged = _enter(text)
    try:
        yield engaged
    finally:
        if engaged:
            if await_drain:
                await _await_playback_drain()
            _exit()


@contextlib.contextmanager
def playback_gate_sync(text: str = "") -> Iterator[bool]:
    """Close the mic for the body. The ``afplay`` subprocess site."""
    engaged = _enter(text)
    try:
        yield engaged
    finally:
        if engaged:
            _exit()


def gate_depth() -> int:
    """Current nesting depth — test/observability seam."""
    with _DEPTH_LOCK:
        return _DEPTH


def force_open() -> None:
    """Emergency release. For teardown paths that must guarantee the operator
    is heard again regardless of what happened."""
    global _DEPTH
    with _DEPTH_LOCK:
        _DEPTH = 0
    try:
        _set_bus_gate(False)
    except BaseException:  # noqa: BLE001 - emergency release, see below
        # The ONE deliberately unbounded catch in this module. force_open is
        # the "make certain the operator can be heard again" path, used by
        # teardown and error recovery; a failure to reopen must never
        # propagate out of it, because the caller is already handling
        # something worse. Every other catch here stays narrow so defects
        # surface — this one exists precisely for the case where they have.
        pass


__all__ = [
    "force_open",
    "gate_depth",
    "gate_enabled",
    "playback_gate",
    "playback_gate_sync",
]
