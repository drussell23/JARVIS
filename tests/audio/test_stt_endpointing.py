"""VAD is an ENDPOINTER, not a gate — the bug that made Karen deaf.

`on_audio_frame` appended a frame ONLY when the VAD called it speech and
dropped it otherwise. webrtcvad in mode 3 — the most aggressive setting, which
this engine selects — rejects the gaps between words, unvoiced consonants
(s, f, th, p, t, k) and the low-energy tails of vowels. The buffer therefore
accumulated speech SHRAPNEL: surviving fragments spliced directly together
with every transition deleted.

It presented perfectly. Speech-level amplitude, speech-shaped spectrum,
plausible duration — and faster-whisper returned "" every single time, because
the audio was no longer language.

Measured on one machine, one microphone, one session:

    a tap keeping EVERY frame   -> "Hello, Karen, testing the microphone path."
    this engine's own buffer    -> "" (zero transcript events)

The VAD decides WHEN an utterance starts and ends. It has no business deciding
which samples inside it survive — speech is continuous, and the quiet parts
carry the consonants.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.voice import streaming_stt as sst


class _Engine(sst.StreamingSTTEngine):
    """Real segmentation, no model and no event loop — the buffer is the
    subject, and loading faster-whisper would make this a model test."""

    def __init__(self, speech_mask):
        super().__init__()
        self._mask = list(speech_mask)
        self._i = 0
        self.scheduled = []
        # on_audio_frame short-circuits unless the engine is running; start()
        # would load faster-whisper, which this test deliberately avoids.
        self._running = True

    def _detect_speech(self, frame):
        v = self._mask[self._i] if self._i < len(self._mask) else False
        self._i += 1
        return bool(v)

    def _schedule_transcription(self, is_partial: bool) -> None:
        with self._buffer_lock:
            audio = (
                np.concatenate(list(self._audio_buffer))
                if self._audio_buffer else np.zeros(0, np.float32)
            )
            if not is_partial:
                self._audio_buffer.clear()
                self._total_frames = 0
        self.scheduled.append(("final" if not is_partial else "partial", audio))


def _frames(n, value=0.5):
    """n frames of 20ms @16k, each a constant so provenance is checkable."""
    return [np.full(320, value, dtype=np.float32) for _ in range(n)]


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_intra_utterance_silence_is_kept():
    """THE REGRESSION. A gap between words must stay in the buffer — that is
    where the consonants live."""
    # speech, gap, speech — the shape of any two-word phrase
    mask = [True] * 5 + [False] * 3 + [True] * 5
    eng = _Engine(mask)
    for f in _frames(13):
        eng.on_audio_frame(f)

    with eng._buffer_lock:
        kept = sum(len(b) for b in eng._audio_buffer)
    assert kept >= 13 * 320, (
        f"kept {kept // 320} of 13 frames — the gap was discarded, which is "
        f"exactly how a phrase becomes unintelligible shrapnel"
    )


def test_the_utterance_is_contiguous_not_spliced():
    """Provenance check: every frame carries its index, so a splice shows up
    as a missing number rather than merely a shorter buffer."""
    mask = [True] * 4 + [False] * 4 + [True] * 4
    eng = _Engine(mask)
    for i in range(12):
        eng.on_audio_frame(np.full(320, (i + 1) / 100.0, dtype=np.float32))

    with eng._buffer_lock:
        audio = np.concatenate(list(eng._audio_buffer))
    present = {round(float(v) * 100) for v in np.unique(audio)}
    assert present >= set(range(1, 13)), f"frames dropped: {present}"


def test_the_onset_is_preserved_by_preroll():
    """VAD needs energy before it fires, so the first phoneme is always
    already past by the time it says 'speech'. Without pre-roll, "hello"
    arrives as "ello"."""
    mask = [False] * 4 + [True] * 6
    eng = _Engine(mask)
    for i in range(10):
        eng.on_audio_frame(np.full(320, (i + 1) / 100.0, dtype=np.float32))

    with eng._buffer_lock:
        audio = np.concatenate(list(eng._audio_buffer))
    present = {round(float(v) * 100) for v in np.unique(audio)}
    assert present & {3, 4}, (
        "no pre-speech audio retained — utterance onsets will be clipped"
    )


# ---------------------------------------------------------------------------
# Endpointing still works
# ---------------------------------------------------------------------------


def test_sustained_silence_ends_the_utterance(monkeypatch):
    monkeypatch.setattr(sst, "_VAD_SILENCE_THRESHOLD_MS", 100)
    times = iter([i * 20.0 for i in range(200)])
    monkeypatch.setattr(sst.time, "time", lambda: next(times) / 1000.0)

    eng = _Engine([True] * 5 + [False] * 20)
    for f in _frames(25):
        eng.on_audio_frame(f)

    assert any(k == "final" for k, _ in eng.scheduled), "utterance never ended"


def test_a_brief_gap_does_not_end_the_utterance(monkeypatch):
    """The whole point: a pause between words is not the end of a sentence."""
    monkeypatch.setattr(sst, "_VAD_SILENCE_THRESHOLD_MS", 600)
    times = iter([i * 20.0 for i in range(200)])
    monkeypatch.setattr(sst.time, "time", lambda: next(times) / 1000.0)

    eng = _Engine([True] * 5 + [False] * 5 + [True] * 5)
    for f in _frames(15):
        eng.on_audio_frame(f)

    assert not any(k == "final" for k, _ in eng.scheduled), (
        "a 100ms inter-word gap ended the turn"
    )


def test_the_final_carries_the_whole_utterance(monkeypatch):
    monkeypatch.setattr(sst, "_VAD_SILENCE_THRESHOLD_MS", 100)
    times = iter([i * 20.0 for i in range(200)])
    monkeypatch.setattr(sst.time, "time", lambda: next(times) / 1000.0)

    eng = _Engine([True] * 10 + [False] * 12)
    for f in _frames(22):
        eng.on_audio_frame(f)

    finals = [a for k, a in eng.scheduled if k == "final"]
    assert finals and len(finals[0]) >= 10 * 320, (
        f"final carried {len(finals[0]) // 320} frames of a 10-frame utterance"
    )


def test_idle_audio_does_not_accumulate_forever():
    """Pre-roll is a bounded ring; silence must not grow the buffer."""
    eng = _Engine([False] * 500)
    for f in _frames(500, value=0.0):
        eng.on_audio_frame(f)

    with eng._buffer_lock:
        assert not eng._audio_buffer, "idle silence leaked into the buffer"
    assert len(eng._preroll) <= eng._preroll.maxlen


def test_vad_no_longer_gates_which_samples_survive():
    """Structural pin. If frame accumulation moves back under `if is_speech`,
    every utterance becomes shrapnel again and nothing else in the suite would
    notice — the amplitude and duration stay plausible."""
    import inspect

    src = inspect.getsource(sst.StreamingSTTEngine.on_audio_frame)
    body = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    # The MAIN accumulation (the last append) must sit under speech-active,
    # not under a per-frame speech test. The earlier append is the pre-roll
    # flush, which is guarded by the onset condition instead.
    i = body.rindex("_audio_buffer.append")
    guard = body[:i]
    assert "if self._speech_active:" in guard, (
        "frames are being accumulated outside the speech-active window again"
    )
    tail = guard[guard.rindex("if self._speech_active:"):]
    assert "if is_speech" not in tail.split("_audio_buffer")[0], (
        "a per-frame VAD test gates accumulation again — that is the gate "
        "behaviour that shredded every utterance"
    )
