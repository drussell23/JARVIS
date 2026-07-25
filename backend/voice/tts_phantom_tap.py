"""Phantom tap — Karen's waveform, synthesized across a process boundary.

Why "phantom"
-------------
Karen's audio never passes through Python at playback time. ``macos_voice.py``
runs ``say -o <temp.aiff>`` then hands the file to ``afplay`` as a **separate OS
process** — a deliberate v283.0 decision, because the previous in-process
PortAudio callback was GIL-dependent and went static during GIL-heavy work
(exactly when the organism is busy, i.e. exactly when Karen talks). There is
therefore no hardware write callback to observe, and reverting that decision to
gain one would trade working audio for a prettier meter.

So the envelope is *synthesized* instead of *tapped*, from two facts that make
it honest rather than fabricated:

* the temp file **is** the audio ``afplay`` will play, so its RMS envelope is
  the true amplitude curve of what the speaker emits;
* ``afplay`` plays at real time, so advancing through that envelope on a clock
  anchored to the ``Popen`` instant tracks the physical output.

Accuracy is bounded by process-launch latency (~10-30ms), not by drift: each
frame is scheduled against an ABSOLUTE deadline derived from the anchor
(``t0 + i/fps``), so a late wakeup does not accumulate — the next frame simply
targets its own true position. This is emphatically **not** a hardware tap and
this module does not claim to be one.

Isolation properties
--------------------
* **Extraction is off-loop.** Decoding a file is blocking work, so it runs in a
  thread via ``asyncio.to_thread`` and yields a small float array. The event
  loop never parses audio.
* **Lifecycle follows the process.** The generator polls the ``afplay`` handle;
  if playback ends or is killed it halts immediately and emits a terminal
  ``0.0`` so the UI flatlines with the speaker rather than freezing on the last
  peak.
* **Nothing in the audio path changes.** No stream is opened, no buffer is
  mutated, no subprocess argument is altered. Failure anywhere here leaves TTS
  byte-identical.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any, AsyncIterator, Callable, List, Optional, Sequence

from backend.voice.audio_decoders import decode_file_blocking, rms_envelope

logger = logging.getLogger(__name__)

#: Envelope framerate. Matches the UI pump's publish cap so the visual cadence
#: is uniform whichever plane is speaking.
DEFAULT_FPS = 20.0


def phantom_tap_enabled() -> bool:
    return os.environ.get(
        "JARVIS_TTS_PHANTOM_TAP_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _fps() -> float:
    try:
        return max(1.0, float(os.environ.get("JARVIS_TTS_ENVELOPE_FPS", str(DEFAULT_FPS))))
    except (TypeError, ValueError):
        return DEFAULT_FPS


# ---------------------------------------------------------------------------
# Envelope extraction (BLOCKING — always called via a thread)
# ---------------------------------------------------------------------------


def _decode_samples(path: str) -> tuple:
    """``(samples, sample_rate)`` via the adaptive decoder, or ``([], 0)``.

    Delegates to :mod:`backend.voice.audio_decoders`, which routes on the
    file's MAGIC NUMBER (not its extension) to whichever backend the host
    actually has. The previous implementation reached for stdlib ``aifc`` +
    ``audioop``; both are DELETED in Python 3.13 (PEP 594), so the visualizer
    carried an expiry date. The native PCM strategy has no dependency and no
    removal horizon."""
    out = decode_file_blocking(path)
    if not out:
        return ([], 0)
    return out


def extract_envelope_blocking(
    path: str, *, fps: Optional[float] = None,
) -> List[float]:
    """RMS envelope at ``fps``, one float per frame. BLOCKING by design — always
    invoke through :func:`extract_envelope`. Returns ``[]`` on any failure, which
    the caller treats as "no visualization for this utterance"."""
    rate = fps if fps else _fps()
    try:
        samples, sr = _decode_samples(path)
        if not samples or sr <= 0:
            return []
        return rms_envelope(samples, sr, rate)
    except Exception:  # noqa: BLE001
        return []


async def extract_envelope(
    path: str, *, fps: Optional[float] = None,
) -> List[float]:
    """Off-loop envelope extraction. Decoding is blocking work, so it runs in a
    worker thread — the event loop never parses audio."""
    try:
        return await asyncio.to_thread(extract_envelope_blocking, path, fps=fps)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Launch-anchored clock, tied to the playback process lifetime
# ---------------------------------------------------------------------------


def _process_alive(proc: Any) -> bool:
    """True while the playback process is still running. A handle we cannot
    interrogate is treated as DEAD — better to stop the animation early than to
    keep painting a waveform after the speaker went quiet."""
    try:
        return proc is not None and proc.poll() is None
    except Exception:  # noqa: BLE001
        return False


async def stream_envelope(
    envelope: Sequence[float],
    *,
    proc: Any = None,
    fps: Optional[float] = None,
    anchor: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], Any]] = None,
) -> AsyncIterator[float]:
    """Yield envelope frames on a clock anchored to the playback launch.

    Frames are scheduled against ABSOLUTE deadlines (``anchor + i/rate``) rather
    than by sleeping a fixed step, so a late wakeup cannot accumulate drift —
    each frame targets its own true position and a badly overdue frame is simply
    emitted at once.

    Terminates early the moment ``proc`` exits, then yields a final ``0.0`` so
    the UI flatlines with the audio instead of freezing on the last peak. That
    terminal flush is emitted on EVERY exit path, including cancellation."""
    rate = fps if fps else _fps()
    now = clock or time.monotonic
    naptime = sleep or asyncio.sleep
    t0 = anchor if anchor is not None else now()
    try:
        for i, level in enumerate(envelope or ()):
            if proc is not None and not _process_alive(proc):
                break
            delay = (t0 + (i / rate)) - now()
            if delay > 0:
                await naptime(delay)
            # Re-check AFTER the wait: playback may have been killed mid-sleep.
            if proc is not None and not _process_alive(proc):
                break
            try:
                yield max(0.0, min(1.0, float(level)))
            except (TypeError, ValueError):
                yield 0.0
    finally:
        # Terminal flush — the UI must return to a flat baseline whether we ran
        # to completion, hit process death, or were cancelled.
        yield 0.0


async def run_phantom_tap(
    path: str,
    *,
    proc: Any = None,
    emit: Optional[Callable[[float], Any]] = None,
    fps: Optional[float] = None,
    anchor: Optional[float] = None,
) -> int:
    """Extract, then stream, feeding each frame to ``emit``.

    Returns how many frames were emitted (including the terminal flush), for
    tests and telemetry. NEVER raises — a failed utterance visualization must
    never disturb speech."""
    if not phantom_tap_enabled():
        return 0
    frames = 0
    try:
        envelope = await extract_envelope(path, fps=fps)
        if not envelope:
            return 0
        async for level in stream_envelope(
            envelope, proc=proc, fps=fps, anchor=anchor,
        ):
            frames += 1
            if emit is not None:
                try:
                    emit(level)
                except Exception:  # noqa: BLE001 — a bad sink never stops speech
                    logger.debug("[PhantomTap] emit failed", exc_info=True)
    except asyncio.CancelledError:
        if emit is not None:
            try:
                emit(0.0)
            except Exception:  # noqa: BLE001
                pass
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[PhantomTap] run failed", exc_info=True)
    return frames


def default_system_emitter() -> Optional[Callable[[float], Any]]:
    """Publish Karen's speaking envelope on BOTH planes she can be watched on.

    In-process, the ``AudioLevelPump`` event bus — correct when the cockpit and
    the audio plane share a process.

    Cross-process, the audio-state socket, tagged ``system`` so the cockpit
    scope swaps cyan → venom green while she talks. THIS is the one that was
    missing. The emitter fed only the in-process bus, but Karen speaks inside
    the audio-plane HOST and the cockpit is a different process, so her
    envelope was published where nobody listens: the operator saw a flat wave
    for the whole time she was speaking, and the venom-green plane — built,
    tested, and reachable — had never once been rendered.

    Exactly the failure that hid the microphone for weeks (a producer with no
    reader across a process boundary), on the other half of the duplex.

    DRY: the socket leg reuses the mic bridge's OWN broadcaster handle and its
    ``publish_rms`` valve — same lossy contract, same 20 FPS budget, same
    frame schema. No second transport, no second normalizer. Missing either
    leg is survivable; a raising emitter would stall TTS, so nothing here
    propagates."""
    try:
        from backend.core.ouroboros.ui.audio_pump import (
            AudioLevelPump, default_publisher,
        )
        from backend.core.ouroboros.ui.audio_scope import AudioPlane

        pump = AudioLevelPump(publish=default_publisher())

        def _emit(level: float) -> None:
            try:
                pump.feed_level(level, plane=AudioPlane.SYSTEM)
            except Exception:  # noqa: BLE001
                pass
            # Cross-process leg. Resolved per-call rather than captured: the
            # broadcaster binds AFTER this emitter is built during boot, and
            # re-binds if the host ever loses its address.
            try:
                from backend.audio.mic_telemetry_bridge import get_bridge
                bridge = get_bridge()
                server = getattr(bridge, "_server", None) if bridge else None
                if server is not None:
                    server.publish_rms(level, "system")
            except Exception:  # noqa: BLE001
                pass

        return _emit
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "DEFAULT_FPS",
    "default_system_emitter",
    "extract_envelope",
    "extract_envelope_blocking",
    "phantom_tap_enabled",
    "run_phantom_tap",
    "stream_envelope",
]
