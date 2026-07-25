"""Phantom tap — Karen's envelope, synthesized across the process boundary.

Karen's audio never enters Python at playback time: `say -o file` then `afplay
file` as a separate OS process (v283.0, deliberately GIL-free). So there is no
hardware callback to tap. The envelope is derived from the very file afplay
plays and advanced on a clock anchored to the Popen instant.

Three mandated assertions:

  (1) file reading happens OFF the event loop (no blocking);
  (2) the generator yields floats synced to a mocked Popen launch;
  (3) killing the mock subprocess aborts the generator and flushes 0.0.
"""

from __future__ import annotations

import asyncio
import math
import struct
import threading
import wave

import pytest

from backend.core.ouroboros.ui.audio_scope import AdaptiveNormalizer, AudioPlane
from backend.voice.tts_phantom_tap import (
    extract_envelope,
    extract_envelope_blocking,
    run_phantom_tap,
    stream_envelope,
)


class _FakeProc:
    """Mimics the subprocess handle contract the tap relies on: poll() is None
    while running, an int once dead."""

    def __init__(self) -> None:
        self._rc = None

    def poll(self):
        return self._rc

    def kill(self) -> None:
        self._rc = -9

    def finish(self) -> None:
        self._rc = 0


def _write_wav(path, *, seconds=1.0, sr=16000, amp=0.5, silent=False):
    n = int(seconds * sr)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            v = 0.0 if silent else math.sin(i / 40.0) * amp
            frames += struct.pack("<h", int(v * 32767))
        fh.writeframes(bytes(frames))
    return str(path)


# ---------------------------------------------------------------------------
# (1) extraction is OFF the event loop
# ---------------------------------------------------------------------------


async def test_extraction_runs_off_the_event_loop(tmp_path):
    """(1) Decoding is blocking work; it must not run on the loop thread."""
    path = _write_wav(tmp_path / "a.wav", seconds=0.5)
    loop_thread = threading.get_ident()
    seen = {}

    real = extract_envelope_blocking

    def _spy(p, **kw):
        seen["thread"] = threading.get_ident()
        return real(p, **kw)

    import backend.voice.tts_phantom_tap as mod
    orig = mod.extract_envelope_blocking
    mod.extract_envelope_blocking = _spy
    try:
        env = await extract_envelope(path, fps=20.0)
    finally:
        mod.extract_envelope_blocking = orig

    assert env, "no envelope extracted"
    assert seen["thread"] != loop_thread, "decoding ran ON the event loop thread"


async def test_event_loop_stays_responsive_during_extraction(tmp_path):
    """The loop must keep servicing other tasks while audio is decoded."""
    path = _write_wav(tmp_path / "b.wav", seconds=1.0, sr=44100)
    ticks = []

    async def _heartbeat():
        for _ in range(20):
            ticks.append(1)
            await asyncio.sleep(0.001)

    await asyncio.gather(extract_envelope(path, fps=20.0), _heartbeat())
    assert len(ticks) == 20, "loop was starved during extraction"


def test_envelope_matches_the_requested_framerate(tmp_path):
    path = _write_wav(tmp_path / "c.wav", seconds=2.0, sr=16000)
    env = extract_envelope_blocking(path, fps=20.0)
    # 2s at 20 FPS ~= 40 frames (final partial window tolerated).
    assert 38 <= len(env) <= 42, f"got {len(env)} frames"
    assert all(0.0 <= v <= 1.0 for v in env)
    assert max(env) > 0.1, "envelope is flat for a non-silent file"


def test_unreadable_file_yields_no_envelope(tmp_path):
    """A failed utterance visualization must degrade, never raise."""
    bad = tmp_path / "not-audio.aiff"
    bad.write_text("this is not audio")
    assert extract_envelope_blocking(str(bad)) == []
    assert extract_envelope_blocking(str(tmp_path / "missing.aiff")) == []


# ---------------------------------------------------------------------------
# (2) launch-anchored generator
# ---------------------------------------------------------------------------


async def test_generator_yields_frames_synced_to_the_launch_anchor():
    """(2) Frames are scheduled against ABSOLUTE deadlines from the anchor."""
    t = {"now": 100.0}
    slept = []

    async def _sleep(d):
        slept.append(d)
        t["now"] += d

    env = [0.2, 0.4, 0.6, 0.8]
    out = []
    async for lvl in stream_envelope(
        env, proc=_FakeProc(), fps=20.0, anchor=100.0,
        clock=lambda: t["now"], sleep=_sleep,
    ):
        out.append(lvl)

    assert out == [0.2, 0.4, 0.6, 0.8, 0.0], "missing frames or terminal flush"
    # 20 FPS -> 50ms spacing; frame 0 is due immediately.
    assert slept == pytest.approx([0.05, 0.05, 0.05], abs=1e-6)


async def test_late_wakeups_do_not_accumulate_drift():
    """A stalled frame must not push every later frame back — each targets its
    own true position, so an overdue frame emits immediately."""
    t = {"now": 0.0}

    async def _sleep(d):
        t["now"] += d

    # Jump the clock far past several frame deadlines before starting.
    t["now"] = 10.0
    waits = []

    async def _tracking_sleep(d):
        waits.append(d)
        t["now"] += d

    out = []
    async for lvl in stream_envelope(
        [0.1] * 5, proc=_FakeProc(), fps=20.0, anchor=0.0,
        clock=lambda: t["now"], sleep=_tracking_sleep,
    ):
        out.append(lvl)

    assert waits == [], "slept despite every deadline already being past"
    assert out == [0.1] * 5 + [0.0]


async def test_empty_envelope_still_flushes():
    out = [lvl async for lvl in stream_envelope([], proc=_FakeProc())]
    assert out == [0.0]


# ---------------------------------------------------------------------------
# (3) process death aborts and flushes
# ---------------------------------------------------------------------------


async def test_killing_the_subprocess_aborts_and_flushes():
    """(3) The UI must flatline WITH the speaker, not freeze on the last peak."""
    proc = _FakeProc()
    t = {"now": 0.0}

    async def _sleep(d):
        t["now"] += d

    out = []
    async for lvl in stream_envelope(
        [0.9] * 100, proc=proc, fps=20.0, anchor=0.0,
        clock=lambda: t["now"], sleep=_sleep,
    ):
        out.append(lvl)
        if len(out) == 3:
            proc.kill()          # speaker goes silent mid-utterance

    assert len(out) < 100, "generator ignored process death"
    assert out[-1] == 0.0, "no terminal flush after abort"
    assert out[:3] == [0.9, 0.9, 0.9]


async def test_already_dead_process_emits_only_the_flush():
    proc = _FakeProc()
    proc.finish()
    out = [lvl async for lvl in stream_envelope([0.5] * 10, proc=proc)]
    assert out == [0.0]


async def test_uninterrogable_handle_is_treated_as_dead():
    """A handle we cannot poll must stop the animation — better early than
    painting a waveform after the audio stopped."""
    class _Broken:
        def poll(self):
            raise OSError("handle is gone")

    out = [lvl async for lvl in stream_envelope([0.5] * 10, proc=_Broken())]
    assert out == [0.0]


async def test_no_proc_runs_to_completion():
    t = {"now": 0.0}

    async def _sleep(d):
        t["now"] += d

    out = [
        lvl async for lvl in stream_envelope(
            [0.3, 0.3], fps=20.0, anchor=0.0,
            clock=lambda: t["now"], sleep=_sleep,
        )
    ]
    assert out == [0.3, 0.3, 0.0]


# ---------------------------------------------------------------------------
# Digital silence must not corrupt the shared normalizer
# ---------------------------------------------------------------------------


def test_absolute_zero_never_divides_by_zero():
    """Unlike a mic, TTS hits absolute 0.0 between words."""
    n = AdaptiveNormalizer()
    for _ in range(500):
        assert n.normalize(0.0) == 0.0          # must not raise
    assert n.peak > 0.0, "peak collapsed to zero"


def test_silence_gap_does_not_rescale_the_meter():
    """THE STROBE BUG this guards: if an inter-syllable gap shrank the peak, the
    next syllable would slam to full scale and Karen would strobe rather than
    breathe."""
    n = AdaptiveNormalizer(hold_frames=40)
    for _ in range(20):
        n.normalize(0.8)                        # a loud syllable
    peak_before = n.peak
    for _ in range(10):
        n.normalize(0.0)                        # a short gap
    assert n.peak == pytest.approx(peak_before), "gap rescaled the reference"

    quiet = n.normalize(0.2)
    assert quiet < 0.5, f"quiet syllable slammed to {quiet}"


def test_sustained_silence_eventually_relaxes_the_reference():
    """After the hold expires a genuinely quieter source must regain range."""
    n = AdaptiveNormalizer(decay=0.5, hold_frames=2)
    n.normalize(1.0)
    for _ in range(60):
        n.normalize(0.0)
    assert n.normalize(0.01) > 0.5, "reference never relaxed"


def test_nan_and_inf_are_rejected():
    n = AdaptiveNormalizer()
    assert n.normalize(float("nan")) == 0.0
    assert n.normalize(float("inf")) == 0.0
    assert n.peak > 0.0


def test_all_zero_file_produces_a_flat_envelope(tmp_path):
    path = _write_wav(tmp_path / "silent.wav", seconds=0.5, silent=True)
    env = extract_envelope_blocking(path, fps=20.0)
    assert env, "silent file should still produce frames"
    assert all(v == 0.0 for v in env)


# ---------------------------------------------------------------------------
# Plane tagging + no buffer mutation
# ---------------------------------------------------------------------------


async def test_broker_payload_carries_the_system_plane(tmp_path):
    """The event must be tagged SYSTEM so the scope swaps cyan -> venom green."""
    from backend.core.ouroboros.ui.audio_pump import AudioLevelPump

    events = []
    pump = AudioLevelPump(
        publish=lambda et, op, p: events.append(p), max_fps=1000.0,
    )

    def _emit(level):
        pump.feed_level(level, plane=AudioPlane.SYSTEM)

    path = _write_wav(tmp_path / "k.wav", seconds=0.2, sr=16000)
    frames = await run_phantom_tap(
        path, proc=None, emit=_emit, fps=20.0, anchor=0.0,
    )

    assert frames > 0
    assert events, "nothing reached the broker"
    assert all(e["plane"] == "system" for e in events), "plane tag missing/wrong"
    assert pump.scope.accent == "venom_green"


async def test_phantom_tap_does_not_mutate_the_playback_file(tmp_path):
    """The tap is read-only: the bytes afplay is playing must be untouched."""
    path = _write_wav(tmp_path / "immutable.wav", seconds=0.2)
    before = open(path, "rb").read()

    await run_phantom_tap(path, proc=None, emit=lambda lvl: None, fps=20.0, anchor=0.0)

    assert open(path, "rb").read() == before, "tap mutated the playback file"


async def test_failing_emitter_never_stops_the_tap(tmp_path):
    path = _write_wav(tmp_path / "e.wav", seconds=0.2)

    def _boom(level):
        raise RuntimeError("UI sink died")

    frames = await run_phantom_tap(
        path, proc=None, emit=_boom, fps=20.0, anchor=0.0,
    )
    assert frames > 0, "a failing sink aborted the tap"


async def test_disabled_tap_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TTS_PHANTOM_TAP_ENABLED", "false")
    path = _write_wav(tmp_path / "off.wav", seconds=0.2)
    hits = []
    assert await run_phantom_tap(path, emit=lambda l: hits.append(l)) == 0
    assert hits == []


def test_playback_seam_is_anchored_at_the_popen(tmp_path):
    """Structural pin: the tap must be launched AFTER the afplay Popen (so the
    anchor is the real playback start) and BEFORE wait() (so it is not delayed
    until playback finishes)."""
    import inspect

    import backend.voice.macos_voice as mv

    src = inspect.getsource(mv)
    i_popen = src.index('["afplay", _temp_path]')
    i_tap = src.index("run_phantom_tap")
    i_wait = src.index("_play_proc.wait()")
    assert i_popen < i_tap < i_wait
