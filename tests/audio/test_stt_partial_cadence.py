"""Partial cadence — the first partial of an utterance was pre-roll.

`_last_partial_time` was never reset when speech began, so the interval test
``now - _last_partial_time > _PARTIAL_INTERVAL_MS`` was already true on the
FIRST speech frame (the previous partial having fired seconds or minutes
earlier, in some other utterance). Every utterance therefore opened by handing
faster-whisper 320 ms of pre-roll room tone plus 20 ms of speech.

Whisper returned nothing, which was correct — it was given 94% silence. The
pipeline then logged "signal has SPEECH-level amplitude: the model rejected
it", wrote a capture-forensics incident, and held the transcription lock long
enough that the first genuinely useful partial 500 ms later was skipped as
busy. In one live session the fingerprint was unmistakable: 334 transcriptions
of exactly ``00:00.340`` — 320 + 20 — the most common duration in the log by
more than 2x, and 4 of the 8 retained incidents.

Coupled to it, the minimum-speech gate measured the whole BUFFER. At a 320 ms
pre-roll against a 300 ms floor, the one guard whose purpose is to keep
sub-speech fragments away from the recogniser was satisfied by pre-roll alone.

Tests 3 and 4 pin behaviour that already worked before this change (post-roll
retention and micro-pause coalescence) so that a future "adaptive buffering"
rewrite cannot quietly remove it.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.voice import streaming_stt as sst

RATE = 16000
FRAME = RATE * 20 // 1000          # 20 ms, the cadence AudioBus delivers


class _Clock:
    """Deterministic wall clock; the engine reads ``time.time()``."""

    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t


class _Probe(sst.StreamingSTTEngine):
    """Real segmentation AND the real ``_schedule_transcription``.

    The sibling suite (``test_stt_endpointing``) stubs the scheduler because
    the buffer is its subject. Here the scheduler IS the subject, so only the
    event loop and the model are replaced — the gate, the pre-roll accounting
    and the clearing all run for real."""

    def __init__(self, mask) -> None:
        super().__init__()
        self._running = True
        self._mask = list(mask)
        self._i = 0
        self.dispatched: list[tuple[bool, int]] = []
        self._transcript_queue = object()      # only checked for None
        self._loop = self                      # stands in for the loop

    # -- event loop stand-in -------------------------------------------
    def call_soon_threadsafe(self, fn):
        fn()

    def create_task(self, x):
        return x

    # -- deterministic VAD ---------------------------------------------
    def _detect_speech(self, frame) -> bool:
        v = self._mask[self._i] if self._i < len(self._mask) else False
        self._i += 1
        return bool(v)

    # -- capture instead of transcribing --------------------------------
    def _run_transcription(self, audio, is_partial):   # type: ignore[override]
        self.dispatched.append((is_partial, len(audio)))
        return None


def _drive(mask, monkeypatch) -> _Probe:
    clock = _Clock()
    monkeypatch.setattr(sst, "time", clock)
    eng = _Probe(mask)
    rng = np.random.default_rng(0)
    for speaking in mask:
        amp = 0.2 if speaking else 0.001
        eng.on_audio_frame((amp * rng.standard_normal(FRAME)).astype(np.float32))
        clock.t += 0.020
    return eng


def _ms(samples: int) -> float:
    return samples / RATE * 1000.0


# --------------------------------------------------------------------------
# 1. the defect
# --------------------------------------------------------------------------

def test_first_partial_is_not_a_preroll_fragment(monkeypatch) -> None:
    eng = _drive([False] * 40 + [True] * 60, monkeypatch)
    partials = [n for p, n in eng.dispatched if p]
    assert partials, "expected at least one partial"

    first = _ms(partials[0])
    preroll_only = sst._PREROLL_MS + 20      # the 00:00.340 fingerprint
    assert first > preroll_only + 1, (
        f"first partial was {first:.0f}ms — pre-roll plus a single frame"
    )
    # It should land one full interval after ONSET, pre-roll included.
    assert first == pytest.approx(
        sst._PREROLL_MS + sst._PARTIAL_INTERVAL_MS, abs=60,
    )


def test_partial_cadence_is_anchored_to_onset_not_the_previous_utterance(
    monkeypatch,
) -> None:
    """Two utterances separated by a long silence. The second must not fire a
    partial on its first frame just because the clock ran on during the gap."""
    mask = ([False] * 40 + [True] * 40 + [False] * 60
            + [True] * 40 + [False] * 60)
    eng = _drive(mask, monkeypatch)
    for is_partial, n in eng.dispatched:
        if is_partial:
            assert _ms(n) > sst._PREROLL_MS + 20 + 1


def test_preroll_alone_cannot_satisfy_the_min_speech_gate(monkeypatch) -> None:
    """One frame of speech behind a 320ms pre-roll is 340ms of buffer and
    20ms of voice. The gate exists to stop exactly this reaching the model."""
    eng = _drive([False] * 40 + [True] * 1 + [False] * 60, monkeypatch)
    for _is_partial, n in eng.dispatched:
        speech = _ms(n) - sst._PREROLL_MS
        assert speech >= sst._MIN_SPEECH_DURATION_MS - 1, (
            f"dispatched {_ms(n):.0f}ms holding only {speech:.0f}ms of speech"
        )


# --------------------------------------------------------------------------
# 2. behaviour that already worked — pinned so a rewrite cannot drop it
# --------------------------------------------------------------------------

def test_post_roll_is_retained_through_the_hangover(monkeypatch) -> None:
    """Frames keep accumulating while ``_speech_active`` holds, so the silence
    that ENDS an utterance is already in the buffer. 400ms of speech yields a
    final of pre-roll + speech + the full endpoint hangover."""
    eng = _drive([False] * 20 + [True] * 20 + [False] * 50, monkeypatch)
    finals = [n for p, n in eng.dispatched if not p]
    assert len(finals) == 1
    total = _ms(finals[0])
    post_roll = total - sst._PREROLL_MS - 400
    assert post_roll >= sst._VAD_SILENCE_THRESHOLD_MS - 40, (
        f"only {post_roll:.0f}ms of post-roll survived"
    )


def test_micro_pause_does_not_split_the_utterance(monkeypatch) -> None:
    """A 200ms gap is shorter than the endpoint hangover, so it must coalesce
    into ONE utterance rather than two fragments."""
    eng = _drive(
        [False] * 20 + [True] * 15 + [False] * 10 + [True] * 15 + [False] * 50,
        monkeypatch,
    )
    finals = [n for p, n in eng.dispatched if not p]
    assert len(finals) == 1, f"micro-pause split into {len(finals)} finals"
    # The gap itself must survive inside the utterance — consonants live there.
    assert _ms(finals[0]) > 15 * 20 + 10 * 20 + 15 * 20


# --------------------------------------------------------------------------
# 3. pre-roll accounting
# --------------------------------------------------------------------------

def test_preroll_accounting_resets_after_a_final(monkeypatch) -> None:
    eng = _drive([False] * 40 + [True] * 40 + [False] * 60, monkeypatch)
    assert eng._preroll_samples == 0


def test_preroll_accounting_shrinks_when_the_cap_evicts_the_head() -> None:
    """The 30s cap evicts from the head, which is where pre-roll sits."""
    eng = sst.StreamingSTTEngine()
    eng._preroll_samples = 3 * FRAME
    eng._max_buffer_frames = 2 * FRAME
    eng._running = True
    eng._speech_active = True
    for _ in range(4):
        with eng._buffer_lock:
            eng._audio_buffer.append(np.zeros(FRAME, np.float32))
            eng._total_frames += FRAME
            while eng._total_frames > eng._max_buffer_frames:
                oldest = eng._audio_buffer.popleft()
                eng._total_frames -= len(oldest)
                eng._preroll_samples = max(
                    0, eng._preroll_samples - len(oldest),
                )
    assert eng._preroll_samples < 3 * FRAME
    assert eng._preroll_samples >= 0
