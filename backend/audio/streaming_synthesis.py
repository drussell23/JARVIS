"""Speak while still generating — an adaptive jitter buffer for TTS.

The latency this removes
------------------------
Today a reply is SERIALIZED: the LLM finishes, then the whole utterance is
synthesized to a file, then afplay plays it. Measured on this machine the
synthesis alone is 1.0-3.2s depending on voice, so the operator hears nothing
for seconds after they stop talking. No amount of LLM speed hides that,
because synthesis happens afterwards.

The fix is to overlap the three stages: sentence N plays while sentence N+1
is being synthesized and N+2 is still arriving from the model. First audio
then lands after the FIRST sentence synthesizes rather than the last.

Why a jitter buffer, and why adaptive
-------------------------------------
Overlapping stages introduces the problem overlapping always introduces: the
producer is irregular. TTS latency here varies by more than 3x between voices
and swings further under load (whisper resident). Start playing too eagerly
and the buffer starves mid-word — the classic robotic stutter. Wait for a
fixed lead and you have simply reintroduced a smaller version of the delay
you set out to remove, and chosen the wrong constant for every machine but
the one it was tuned on.

So the lead is MEASURED. An EWMA tracks both the mean arrival interval and
its variance, and the prefetch threshold is ``mean + k·stddev`` — the buffer
grows itself exactly as far as the observed jitter demands and shrinks again
when the producer steadies. A machine with fast, regular synthesis converges
on a short lead; a loaded one holds more. Neither is configured.

Why zero-crossing stitching
---------------------------
Consecutive TTS segments are synthesized independently, so segment N can end
at amplitude +0.4 while N+1 begins at -0.3. Concatenating them puts a step
discontinuity in the waveform, and a step is broadband: it is heard as a
click at every boundary. The ring buffer cannot help — it is sample-
continuous by construction, and faithfully reproduces the step it was given.

The fix belongs where the segments meet: trim each segment's tail forward and
its head backward to the nearest zero crossing, so consecutive segments abut
at ~0 amplitude and the seam is inaudible. A few milliseconds are discarded
at each boundary, which is far below the threshold of perception and far
above the audibility of a click.

Preemption
----------
Barge-in must stop her mid-word, not at the end of the current segment. The
cancel event is checked before every synthesis, before every yield, and
between chunks; on trip the ring buffer is FLUSHED (queued audio is already
committed and would otherwise keep playing after the interrupt), the
generator is closed so no further synthesis is scheduled, and the caller
transitions to idle. Nothing is left running.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def streaming_enabled() -> bool:
    """Master gate. OFF restores the serialized synthesize-then-play path
    byte-for-byte, which is the only honest way to compare them."""
    return os.getenv("JARVIS_TTS_STREAMING_ENABLED", "true").strip().lower() in _TRUTHY


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# Adaptive jitter buffer
# ---------------------------------------------------------------------------


@dataclass
class JitterStats:
    """EWMA of inter-arrival time and its variance.

    Welford-style online variance adapted to an exponentially weighted window,
    so the estimate follows the CURRENT producer rather than averaging over a
    session in which conditions changed. That matters here: synthesis speed
    changes materially when whisper loads, when a different voice is elected,
    or when the machine is under memory pressure.
    """

    alpha: float = 0.25          # weight of the newest sample
    mean_s: float = 0.0
    var_s2: float = 0.0
    samples: int = 0

    def observe(self, interval_s: float) -> None:
        """Fold one inter-arrival interval into the estimate."""
        try:
            x = max(0.0, float(interval_s))
        except (TypeError, ValueError):
            return
        if self.samples == 0:
            self.mean_s = x
            self.var_s2 = 0.0
        else:
            delta = x - self.mean_s
            self.mean_s += self.alpha * delta
            # EWMA of squared deviation, evaluated against the PREVIOUS mean
            # so a single outlier inflates variance (correctly) rather than
            # being absorbed into the mean and hidden.
            self.var_s2 = (1 - self.alpha) * (self.var_s2 + self.alpha * delta * delta)
        self.samples += 1

    @property
    def stddev_s(self) -> float:
        return math.sqrt(max(0.0, self.var_s2))

    def lead_s(self, *, k: float, floor_s: float, ceiling_s: float) -> float:
        """Seconds of audio to hold before starting playback.

        ``mean + k·stddev``: enough lead to cover the arrival gap plus the
        observed irregularity in it. With no samples yet the floor applies —
        the first utterance cannot be predicted from nothing, and guessing
        high would reintroduce the delay this exists to remove."""
        if self.samples == 0:
            return floor_s
        return max(floor_s, min(ceiling_s, self.mean_s + k * self.stddev_s))


class AdaptiveJitterBuffer:
    """Holds synthesized audio until there is enough lead to play smoothly.

    Not a fixed queue depth: the threshold is recomputed from live arrival
    statistics after every segment, so the buffer is as shallow as the
    producer allows and as deep as it requires.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        k: Optional[float] = None,
        floor_s: Optional[float] = None,
        ceiling_s: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.stats = JitterStats(alpha=_env_float("JARVIS_TTS_JITTER_ALPHA", 0.25))
        #: How many standard deviations of headroom. 2 covers ~95% of arrivals
        #: for a roughly normal producer; it is a confidence choice, not a
        #: duration, so it does not need per-machine tuning.
        self._k = k if k is not None else _env_float("JARVIS_TTS_JITTER_K", 2.0)
        #: Never wait less than this before the first sample — below a frame
        #: or two the ring buffer cannot absorb any scheduling noise at all.
        self._floor = floor_s if floor_s is not None else _env_float(
            "JARVIS_TTS_JITTER_FLOOR_S", 0.12,
        )
        #: Never wait more than this, however erratic the producer. Past ~1.5s
        #: the operator is waiting again and smoothness has stopped being the
        #: thing that matters.
        self._ceiling = ceiling_s if ceiling_s is not None else _env_float(
            "JARVIS_TTS_JITTER_CEILING_S", 1.5,
        )
        self._clock = clock or time.monotonic
        self._pending: List[np.ndarray] = []
        self._held_samples = 0
        self._last_arrival: Optional[float] = None
        self._started = False

    # -- producer side ---------------------------------------------------

    def offer(self, chunk: np.ndarray) -> None:
        """Add a synthesized segment and fold its arrival into the stats."""
        now = self._clock()
        if self._last_arrival is not None:
            self.stats.observe(now - self._last_arrival)
        self._last_arrival = now
        if chunk is not None and len(chunk):
            self._pending.append(chunk)
            self._held_samples += len(chunk)

    def mark_producer_done(self) -> None:
        """No more segments are coming — release regardless of lead."""
        self._started = True

    # -- consumer side ---------------------------------------------------

    @property
    def lead_target_s(self) -> float:
        return self.stats.lead_s(
            k=self._k, floor_s=self._floor, ceiling_s=self._ceiling,
        )

    @property
    def held_s(self) -> float:
        return self._held_samples / float(self.sample_rate)

    def ready(self) -> bool:
        """Is there enough audio held to begin (or continue) playing?

        Once playback has STARTED the buffer releases everything it has: the
        lead exists to survive the next gap, and re-accumulating it mid-
        utterance would insert exactly the silence it was built to prevent."""
        if self._started:
            return bool(self._pending)
        if self.held_s >= self.lead_target_s:
            self._started = True
            return True
        return False

    def drain(self) -> List[np.ndarray]:
        """Take everything currently held."""
        out, self._pending = self._pending, []
        self._held_samples = 0
        return out

    def flush(self) -> int:
        """Discard held audio (barge-in). Returns samples dropped."""
        dropped = self._held_samples
        self._pending.clear()
        self._held_samples = 0
        return dropped


# ---------------------------------------------------------------------------
# Zero-crossing stitching
# ---------------------------------------------------------------------------


def _find_zero_crossing(
    audio: np.ndarray, *, from_end: bool, max_search: int,
) -> int:
    """Index of the nearest sign change within *max_search* samples.

    Returns the boundary index to cut at: for ``from_end`` the length to keep,
    otherwise the offset to start from. Falls back to no trim when the segment
    is short or never crosses — trimming blindly would remove real audio to
    prevent a click that may not exist."""
    n = len(audio)
    if n < 4 or max_search <= 0:
        return n if from_end else 0
    window = min(max_search, n - 1)
    if from_end:
        seg = audio[n - window:]
        signs = np.signbit(seg)
        changes = np.flatnonzero(signs[1:] != signs[:-1])
        if not len(changes):
            return n
        return int(n - window + changes[-1] + 1)
    seg = audio[:window]
    signs = np.signbit(seg)
    changes = np.flatnonzero(signs[1:] != signs[:-1])
    if not len(changes):
        return 0
    return int(changes[0] + 1)


def _equal_power_crossfade(
    left: np.ndarray, right: np.ndarray, n: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Overlap the last *n* samples of *left* with the first *n* of *right*.

    The zero-crossing pass is exact and colours nothing, but it can only work
    if a crossing EXISTS inside the search window. Low-frequency content — a
    held vowel, a voice with a deep fundamental — can run longer than the
    window without crossing zero, and the fallback was to leave the step
    alone, which leaves the click this function exists to remove.

    So a short equal-power fade is the backstop. ``sqrt`` ramps keep the
    summed power constant through the overlap (a linear fade dips ~3dB in the
    middle and is audible as a momentary thinning), and a few milliseconds is
    far below the threshold at which a listener perceives the crossfade
    itself."""
    n = int(min(n, len(left), len(right)))
    if n <= 1:
        return left, right
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    fade_out = np.sqrt(1.0 - t, dtype=np.float32)
    fade_in = np.sqrt(t, dtype=np.float32)
    blended = left[-n:] * fade_out + right[:n] * fade_in
    return (
        np.concatenate([left[:-n], blended]).astype(np.float32),
        right[n:].astype(np.float32),
    )


def stitch_zero_crossing(
    chunks: List[np.ndarray],
    *,
    sample_rate: int,
    max_trim_ms: float = 8.0,
    crossfade_ms: float = 3.0,
    step_tolerance: float = 0.02,
) -> np.ndarray:
    """Join segments so consecutive ones abut at ~0 amplitude.

    Independently synthesized segments end and begin at arbitrary amplitudes.
    Concatenating +0.4 directly onto -0.3 puts a STEP in the waveform, and a
    step is broadband — heard as a click at every seam. Trimming each side to
    its nearest zero crossing removes a few milliseconds (inaudible) to remove
    the discontinuity (very audible).

    NEVER raises; returns an empty array for empty input."""
    usable = [c for c in (chunks or []) if c is not None and len(c)]
    if not usable:
        return np.zeros(0, dtype=np.float32)
    if len(usable) == 1:
        return usable[0].astype(np.float32, copy=False)

    max_search = max(1, int(sample_rate * max_trim_ms / 1000.0))
    fade_n = max(2, int(sample_rate * crossfade_ms / 1000.0))

    # Pass 1 — zero-crossing alignment. Exact, colours nothing, and usually
    # sufficient: speech crosses zero every 2-6ms at a normal fundamental.
    trimmed: List[np.ndarray] = []
    for i, chunk in enumerate(usable):
        c = np.asarray(chunk, dtype=np.float32)
        if i > 0:
            c = c[_find_zero_crossing(c, from_end=False, max_search=max_search):]
        if i < len(usable) - 1:
            c = c[:_find_zero_crossing(c, from_end=True, max_search=max_search)]
        if len(c):
            trimmed.append(c)
    if not trimmed:
        return np.zeros(0, dtype=np.float32)

    # Pass 2 — measure what pass 1 actually achieved and fade only the seams
    # it could not fix. Checking rather than assuming is the point: a window
    # with no crossing in it silently leaves the discontinuity, and a stitcher
    # that reports success while emitting clicks is worse than none.
    out = [trimmed[0]]
    for nxt in trimmed[1:]:
        prev = out[-1]
        step = abs(float(prev[-1]) - float(nxt[0])) if len(prev) and len(nxt) else 0.0
        if step > step_tolerance:
            prev, nxt = _equal_power_crossfade(prev, nxt, fade_n)
            out[-1] = prev
        if len(nxt):
            out.append(nxt)
    return np.concatenate(out).astype(np.float32)


def boundary_step(joined: np.ndarray, boundaries: List[int]) -> float:
    """Largest amplitude step at the given seam indices — the click metric."""
    worst = 0.0
    for b in boundaries:
        if 0 < b < len(joined):
            worst = max(worst, abs(float(joined[b] - joined[b - 1])))
    return worst


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass
class StreamingResult:
    """What happened, for honest logging and tests."""

    segments: int = 0
    first_audio_s: float = -1.0
    cancelled: bool = False
    starved: int = 0
    lead_used_s: float = 0.0
    text: str = field(default="")


async def stream_synthesis(
    sentences: AsyncIterator[str],
    *,
    synthesize: Callable[[str], Any],
    play: Callable[[AsyncIterator[np.ndarray], int], Any],
    sample_rate: int = 22050,
    cancel: Optional[asyncio.Event] = None,
    clock: Optional[Callable[[], float]] = None,
) -> StreamingResult:
    """Synthesize and play sentences with overlap. NEVER raises.

    ``synthesize`` is awaited per sentence and must return float32 audio; it
    runs off the event loop (the caller supplies an executor-backed callable),
    because a blocking synthesis here would freeze the terminal UI, the live
    indicator and the waveform — the whole point of mandate 3.

    ``play`` receives an async iterator of chunks, matching
    ``AudioBus.play_stream``'s existing signature so the sink, the pacing and
    the resampling are reused rather than reimplemented.
    """
    _clock = clock or time.monotonic
    started_at = _clock()
    result = StreamingResult()
    buf = AdaptiveJitterBuffer(sample_rate=sample_rate, clock=_clock)
    spoken: List[str] = []

    async def _chunks() -> AsyncIterator[np.ndarray]:
        """Consumer: releases held audio once the adaptive lead is met."""
        nonlocal result
        starving = False
        while True:
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                return
            if buf.ready():
                starving = False
                for piece in buf.drain():
                    if cancel is not None and cancel.is_set():
                        result.cancelled = True
                        return
                    if result.first_audio_s < 0:
                        result.first_audio_s = _clock() - started_at
                        result.lead_used_s = buf.lead_target_s
                    yield piece
            elif producer_done.is_set() and not buf._pending:
                return
            else:
                # Count starvation EDGES, not polls. Incrementing per 10ms
                # tick reported 89 "starvations" for a single continuous wait,
                # which would have made the metric useless for deciding
                # whether the adaptive lead is working — the one question it
                # exists to answer.
                if buf._started and not starving:
                    starving = True
                    result.starved += 1
                # Yield to the loop rather than spinning: the UI, the RMS
                # stream and the state indicator all live on this loop.
                await asyncio.sleep(0.01)

    producer_done = asyncio.Event()

    async def _produce() -> None:
        try:
            async for sentence in sentences:
                if cancel is not None and cancel.is_set():
                    result.cancelled = True
                    return
                text = (sentence or "").strip()
                if not text:
                    continue
                try:
                    audio = await synthesize(text)
                except (RuntimeError, OSError, ValueError) as exc:
                    logger.warning("[StreamSynth] segment failed: %r", exc)
                    continue
                if audio is None or not len(audio):
                    continue
                buf.offer(np.asarray(audio, dtype=np.float32))
                spoken.append(text)
                result.segments += 1
        finally:
            producer_done.set()
            buf.mark_producer_done()

    producer = asyncio.get_running_loop().create_task(_produce())
    try:
        await play(_chunks(), sample_rate)
    except asyncio.CancelledError:
        result.cancelled = True
        raise
    except (RuntimeError, OSError, ValueError) as exc:
        logger.warning("[StreamSynth] playback failed: %r", exc)
    finally:
        # Preemption: stop scheduling work and drop what is still held. A
        # generator left running would keep synthesizing into a buffer nobody
        # will read, and held audio would keep playing after the interrupt.
        if not producer.done():
            producer.cancel()
            try:
                await producer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        dropped = buf.flush()
        if dropped and result.cancelled:
            logger.debug("[StreamSynth] flushed %d queued samples on cancel", dropped)

    result.text = " ".join(spoken)
    return result


# ---------------------------------------------------------------------------
# Piped synthesis — audio arrives WHILE the synthesizer is still working
# ---------------------------------------------------------------------------


def _caf_audio_offset(head: bytes) -> int:
    """Byte offset of the first audio sample in a CAF stream, or -1.

    CAF is a chunked container: ``caff`` magic, then typed chunks each with an
    8-byte type and a big-endian int64 size. Audio lives in ``data`` after a
    4-byte edit count. The offset is NOT constant — ``say`` emits a ``free``
    padding chunk whose size varies — so it is discovered by walking the
    chunk list rather than assumed, which is the difference between reading
    samples and reading a header as if it were audio."""
    import struct

    if len(head) < 12 or head[:4] != b"caff":
        return -1
    off = 8
    while off + 12 <= len(head):
        ctype = head[off:off + 4]
        try:
            csize = struct.unpack(">q", head[off + 4:off + 12])[0]
        except struct.error:
            return -1
        if ctype == b"data":
            return off + 12 + 4
        if csize < 0:
            return -1
        off += 12 + csize
    return -1


async def piped_say(
    text: str,
    *,
    voice_args: Optional[List[str]] = None,
    sample_rate: int = 22050,
    chunk_frames: int = 2048,
    cancel: Optional[asyncio.Event] = None,
) -> AsyncIterator[np.ndarray]:
    """Yield float32 audio from ``say`` AS IT IS GENERATED. NEVER raises.

    `say` cannot write to a pipe — ``-o -`` and ``-o /dev/stdout`` are both
    refused, and a FIFO fails because CAF seeks back to patch its header. But
    it DOES write its output file incrementally: measured here, first bytes
    at 1.72s into an 8.63s synthesis and growing steadily throughout. So the
    file is followed as it grows rather than waited on, which is the whole
    difference between hearing the first words in under a second and hearing
    nothing until the last word is rendered.

    The temp file is an implementation detail of the OS tool, not a buffering
    stage: nothing waits for it to be complete, and it is unlinked on exit.
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".caf", prefix="jarvis_tts_stream_")
    os.close(fd)
    proc = None
    try:
        args = ["say", "--file-format=caff", f"--data-format=LEF32@{sample_rate}"]
        args += list(voice_args or [])
        args += ["-o", path, text]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        audio_off = -1
        read_pos = 0
        bytes_per_chunk = max(256, int(chunk_frames)) * 4
        idle_polls = 0
        while True:
            if cancel is not None and cancel.is_set():
                return
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0

            if audio_off < 0:
                if size >= 64:
                    with open(path, "rb") as f:
                        audio_off = _caf_audio_offset(f.read(min(size, 65536)))
                    if audio_off >= 0:
                        read_pos = audio_off
            elif size - read_pos >= bytes_per_chunk:
                with open(path, "rb") as f:
                    f.seek(read_pos)
                    blob = f.read(((size - read_pos) // 4) * 4)
                if blob:
                    read_pos += len(blob)
                    idle_polls = 0
                    yield np.frombuffer(blob, dtype="<f4").astype(np.float32)
                    continue

            done = proc.returncode is not None
            if done:
                # Final partial read: the tail is smaller than a chunk and
                # would otherwise be dropped — which is the last syllable.
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                if audio_off >= 0 and size > read_pos:
                    with open(path, "rb") as f:
                        f.seek(read_pos)
                        blob = f.read(((size - read_pos) // 4) * 4)
                    if blob:
                        yield np.frombuffer(blob, dtype="<f4").astype(np.float32)
                return
            idle_polls += 1
            if idle_polls > 2000:            # ~20s with no progress
                logger.warning("[PipedSay] no output progress — abandoning")
                return
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError) as exc:
        logger.warning("[PipedSay] streaming synthesis failed: %r", exc)
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        try:
            os.unlink(path)
        except OSError:
            pass


__all__ = [
    "AdaptiveJitterBuffer",
    "piped_say",
    "JitterStats",
    "StreamingResult",
    "boundary_step",
    "stitch_zero_crossing",
    "stream_synthesis",
    "streaming_enabled",
]
