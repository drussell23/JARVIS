"""Speak while still generating, without stuttering or clicking.

A reply is currently SERIALIZED: LLM finishes -> whole utterance synthesizes
-> afplay plays it. Synthesis alone measures 1.0-3.2s here depending on voice,
so the operator hears nothing for seconds after they stop talking.

Overlapping the stages fixes that and introduces two classic failure modes
this suite exists to pin: buffer starvation (robotic stutter) and amplitude
discontinuity at segment seams (clicking).
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from backend.audio.streaming_synthesis import (
    AdaptiveJitterBuffer,
    JitterStats,
    boundary_step,
    stitch_zero_crossing,
    stream_synthesis,
)

SR = 22050


def _tone(seconds=0.2, freq=140.0, phase=0.0, amp=0.4, sr=SR):
    t = np.linspace(0, seconds, int(sr * seconds), dtype=np.float32)
    return (np.sin(2 * np.pi * freq * t + phase) * amp
            + np.sin(2 * np.pi * 430 * t + phase) * amp * 0.5).astype(np.float32)


# ---------------------------------------------------------------------------
# Task 1 — the buffer adapts, it is not configured
# ---------------------------------------------------------------------------


def test_a_steady_producer_converges_on_a_short_lead():
    """A fast, regular machine must not be made to wait for a constant chosen
    on somebody else's."""
    t = {"n": 0.0}
    b = AdaptiveJitterBuffer(sample_rate=SR, clock=lambda: t["n"])
    for _ in range(12):
        t["n"] += 0.10
        b.offer(np.zeros(160, dtype=np.float32))
    assert b.stats.stddev_s < 0.01
    assert b.lead_target_s <= 0.15


def test_an_erratic_producer_grows_the_lead():
    """THE STUTTER CASE. Irregular arrivals must buy more headroom, or the
    buffer starves mid-word."""
    t = {"n": 0.0}
    b = AdaptiveJitterBuffer(sample_rate=SR, clock=lambda: t["n"])
    for iv in (0.1, 0.9, 0.15, 1.2, 0.12, 0.8):
        t["n"] += iv
        b.offer(np.zeros(160, dtype=np.float32))
    assert b.stats.stddev_s > 0.2
    assert b.lead_target_s > 0.5, "no extra headroom bought for real jitter"


def test_the_lead_is_bounded_at_both_ends():
    """Past ~1.5s the operator is waiting again and smoothness has stopped
    being the thing that matters."""
    t = {"n": 0.0}
    b = AdaptiveJitterBuffer(sample_rate=SR, clock=lambda: t["n"])
    for _ in range(8):
        t["n"] += 5.0
        b.offer(np.zeros(160, dtype=np.float32))
    assert b.lead_target_s <= 1.5

    fresh = AdaptiveJitterBuffer(sample_rate=SR)
    assert fresh.lead_target_s >= 0.05, "no floor — cannot absorb any jitter"


def test_the_lead_shrinks_again_when_the_producer_steadies():
    """EWMA, not a running average: the estimate must follow the CURRENT
    producer, because synthesis speed changes when whisper loads or the voice
    changes."""
    t = {"n": 0.0}
    b = AdaptiveJitterBuffer(sample_rate=SR, clock=lambda: t["n"])
    for iv in (1.0, 0.05, 1.0, 0.05):
        t["n"] += iv
        b.offer(np.zeros(160, dtype=np.float32))
    erratic = b.lead_target_s
    for _ in range(30):
        t["n"] += 0.1
        b.offer(np.zeros(160, dtype=np.float32))
    assert b.lead_target_s < erratic, "the buffer never recovered"


def test_playback_does_not_re_accumulate_lead_mid_utterance():
    """Once started, the buffer releases everything it has. Rebuilding the
    lead mid-sentence would insert exactly the silence it exists to prevent."""
    b = AdaptiveJitterBuffer(sample_rate=SR, floor_s=1.0)
    b.offer(np.zeros(SR, dtype=np.float32))          # 1.0s -> starts
    assert b.ready() is True
    b.drain()
    b.offer(np.zeros(160, dtype=np.float32))         # a sliver
    assert b.ready() is True, "re-gated after starting"


def test_a_finished_producer_releases_regardless_of_lead():
    """The last words of a reply must not be held hostage to a lead that will
    never be met."""
    b = AdaptiveJitterBuffer(sample_rate=SR, floor_s=5.0)
    b.offer(np.zeros(160, dtype=np.float32))
    assert b.ready() is False
    b.mark_producer_done()
    assert b.ready() is True


def test_variance_is_not_hidden_by_the_mean():
    s = JitterStats()
    for x in (0.1, 0.1, 0.1, 2.0):
        s.observe(x)
    assert s.stddev_s > 0.1, "an outlier vanished into the mean"


# ---------------------------------------------------------------------------
# Task 2 — seams must not click
# ---------------------------------------------------------------------------


def test_a_seam_between_independent_segments_is_smoothed():
    """THE CLICK CASE. Segment N ends at +0.4, N+1 starts at -0.3; naive
    concatenation puts a step in the waveform and a step is broadband."""
    a, c = _tone(phase=0.0), _tone(phase=1.9)
    naive = np.concatenate([a, c])
    naive_step = abs(float(naive[len(a)] - naive[len(a) - 1]))
    assert naive_step > 0.1, "test signal has no seam to fix"

    joined = stitch_zero_crossing([a, c], sample_rate=SR)
    assert float(np.abs(np.diff(joined)).max()) < naive_step / 4, (
        "the seam still steps — this is audible as a click"
    )


def test_low_frequency_content_without_a_crossing_still_gets_smoothed():
    """The zero-crossing pass can only work if a crossing EXISTS in the search
    window. A held vowel or a deep fundamental may not cross, and the first
    implementation silently left the click — measured 0 samples trimmed and
    the step unchanged. The crossfade backstop covers it."""
    a = (np.sin(np.linspace(0, 9.3, 4410)) * 0.5).astype(np.float32)
    c = (np.sin(np.linspace(2.1, 11.0, 4410)) * 0.5).astype(np.float32)
    naive_step = abs(float(c[0] - a[-1]))
    joined = stitch_zero_crossing([a, c], sample_rate=SR)
    assert float(np.abs(np.diff(joined)).max()) < naive_step / 4


def test_stitching_discards_only_a_few_milliseconds():
    """Inaudible to remove something very audible — but it must stay small."""
    a, c = _tone(), _tone(phase=2.0)
    joined = stitch_zero_crossing([a, c], sample_rate=SR)
    lost_ms = (len(a) + len(c) - len(joined)) / SR * 1000
    assert 0 <= lost_ms < 25, f"discarded {lost_ms:.1f}ms at one seam"


def test_a_single_segment_is_returned_untouched():
    a = _tone()
    assert np.array_equal(stitch_zero_crossing([a], sample_rate=SR), a)


def test_stitching_handles_degenerate_input():
    assert len(stitch_zero_crossing([], sample_rate=SR)) == 0
    assert len(stitch_zero_crossing([np.zeros(0, np.float32)], sample_rate=SR)) == 0
    out = stitch_zero_crossing([_tone(), np.zeros(0, np.float32), _tone()], sample_rate=SR)
    assert len(out) > 0


def test_the_crossfade_is_equal_power():
    """A linear fade dips ~3dB mid-overlap and is heard as a momentary
    thinning; sqrt ramps hold summed power constant."""
    from backend.audio.streaming_synthesis import _equal_power_crossfade

    n = 256
    left = np.ones(n * 2, dtype=np.float32)
    right = np.ones(n * 2, dtype=np.float32)
    merged, _ = _equal_power_crossfade(left, right, n)
    overlap = merged[-n:]
    assert float(overlap.min()) > 0.98, "power dipped through the crossfade"


# ---------------------------------------------------------------------------
# Task 3 — non-blocking, and preemptible
# ---------------------------------------------------------------------------


async def test_first_audio_lands_before_the_last_segment_synthesizes():
    """THE WHOLE POINT. Serialized, nothing is heard until every segment is
    synthesized; overlapped, the first plays while the rest are still coming."""
    async def sentences():
        for s in ("one", "two", "three"):
            yield s

    async def synth(_t):
        await asyncio.sleep(0.3)
        return _tone(seconds=0.3)

    async def play(chunks, _rate):
        async for _ in chunks:
            pass

    result = await stream_synthesis(
        sentences(), synthesize=synth, play=play, sample_rate=SR,
    )
    assert result.segments == 3
    assert 0 < result.first_audio_s < 3 * 0.3, (
        f"first audio at {result.first_audio_s:.2f}s — no overlap achieved"
    )


async def test_the_event_loop_stays_responsive_during_playback():
    """Mandate 3: playback must not freeze the terminal UI, the live
    indicator or the waveform — all of which live on this loop."""
    ticks = {"n": 0}

    async def ui():
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def sentences():
        for s in ("a", "b"):
            yield s

    async def synth(_t):
        await asyncio.sleep(0.2)
        return _tone(seconds=0.2)

    async def play(chunks, _rate):
        async for _ in chunks:
            await asyncio.sleep(0.05)

    task = asyncio.get_running_loop().create_task(ui())
    try:
        await stream_synthesis(sentences(), synthesize=synth, play=play, sample_rate=SR)
    finally:
        task.cancel()
    assert ticks["n"] > 10, f"the loop stalled — only {ticks['n']} ticks"


async def test_barge_in_stops_the_generator_and_leaves_no_zombies():
    """Preemption must halt synthesis, drop held audio, and leave nothing
    running — a generator left alive keeps synthesizing into a buffer nobody
    will read."""
    cancel = asyncio.Event()
    synthesized = {"n": 0}

    async def sentences():
        for s in ("one", "two", "three", "four", "five"):
            yield s

    async def synth(_t):
        synthesized["n"] += 1
        await asyncio.sleep(0.05)
        return _tone(seconds=0.1)

    async def play(chunks, _rate):
        async for _ in chunks:
            cancel.set()                      # interrupt on the first chunk

    before = len(asyncio.all_tasks())
    result = await stream_synthesis(
        sentences(), synthesize=synth, play=play, sample_rate=SR, cancel=cancel,
    )
    await asyncio.sleep(0.2)

    assert result.cancelled is True
    assert synthesized["n"] < 5, "kept synthesizing after the interrupt"
    assert len(asyncio.all_tasks()) <= before, "a task was left running"


async def test_a_failing_segment_does_not_abort_the_reply():
    """One bad synthesis costs a sentence, not the conversation."""
    async def sentences():
        for s in ("good", "bad", "good again"):
            yield s

    async def synth(text):
        if text == "bad":
            raise RuntimeError("synthesis exploded")
        return _tone(seconds=0.05)

    async def play(chunks, _rate):
        async for _ in chunks:
            pass

    result = await stream_synthesis(
        sentences(), synthesize=synth, play=play, sample_rate=SR,
    )
    assert result.segments == 2


async def test_starvation_is_counted_as_edges_not_polls():
    """Incrementing per 10ms tick reported 89 starvations for ONE continuous
    wait, which makes the metric useless for judging whether the adaptive
    lead is working — the only question it exists to answer."""
    async def sentences():
        for s in ("a", "b", "c"):
            yield s

    async def synth(_t):
        await asyncio.sleep(0.3)
        return _tone(seconds=0.05)

    async def play(chunks, _rate):
        async for _ in chunks:
            pass

    result = await stream_synthesis(
        sentences(), synthesize=synth, play=play, sample_rate=SR,
    )
    assert result.starved <= result.segments, (
        f"starvation over-counted: {result.starved} for {result.segments} segments"
    )


# ---------------------------------------------------------------------------
# Piped synthesis — audio while the synthesizer is still working
# ---------------------------------------------------------------------------
#
# `say` cannot write to a pipe: `-o -` and `-o /dev/stdout` are both refused,
# and a FIFO fails because CAF seeks back to patch its header. But it DOES
# write its output file incrementally — measured, first bytes 1.72s into an
# 8.63s synthesis and growing steadily. Following the file as it grows is the
# difference between 1.9s and 6.2s to first audio on a long reply.


def test_the_caf_audio_offset_is_discovered_not_assumed():
    """`say` emits a `free` padding chunk of varying size, so the audio
    offset moves. Assuming a constant would read header bytes as samples."""
    import struct

    from backend.audio.streaming_synthesis import _caf_audio_offset

    def _caf(free_size: int) -> bytes:
        out = b"caff" + b"\x00\x01\x00\x00"
        out += b"desc" + struct.pack(">q", 32) + b"\x00" * 32
        out += b"free" + struct.pack(">q", free_size) + b"\x00" * free_size
        out += b"data" + struct.pack(">q", 100) + b"\x00" * 4
        return out

    small, large = _caf(16), _caf(4016)
    assert _caf_audio_offset(small) != _caf_audio_offset(large)
    assert _caf_audio_offset(large) == 8 + 12 + 32 + 12 + 4016 + 12 + 4


def test_a_non_caf_stream_is_rejected_rather_than_misread():
    from backend.audio.streaming_synthesis import _caf_audio_offset

    assert _caf_audio_offset(b"RIFFxxxxWAVE") == -1
    assert _caf_audio_offset(b"") == -1
    assert _caf_audio_offset(b"caff") == -1


async def test_piped_say_yields_before_synthesis_completes():
    """THE POINT. Audio must arrive while `say` is still running."""
    from backend.audio.streaming_synthesis import piped_say

    text = "Hello Derek. " * 10
    first = None
    chunks = 0
    t0 = asyncio.get_running_loop().time()
    async for chunk in piped_say(text, sample_rate=22050):
        if first is None:
            first = asyncio.get_running_loop().time() - t0
        chunks += 1
        assert chunk.dtype == np.float32
    total = asyncio.get_running_loop().time() - t0
    if chunks == 0:
        pytest.skip("`say` unavailable in this environment")
    assert first < total * 0.75, (
        f"first audio at {first:.2f}s of a {total:.2f}s synthesis — no overlap"
    )


async def test_piped_say_stops_promptly_on_cancel():
    """Barge-in must kill the synthesizer, not wait it out."""
    from backend.audio.streaming_synthesis import piped_say

    cancel = asyncio.Event()
    got = 0
    async for _chunk in piped_say("Hello Derek. " * 30, sample_rate=22050,
                                  cancel=cancel):
        got += 1
        cancel.set()
    assert got <= 2, f"kept yielding {got} chunks after cancel"


async def test_piped_say_never_raises_on_a_bad_voice():
    """An unusable voice must cost the utterance, not the conversation."""
    from backend.audio.streaming_synthesis import piped_say

    out = [c async for c in piped_say(
        "hello", voice_args=["-v", "NoSuchVoiceAnywhere"], sample_rate=22050,
    )]
    assert isinstance(out, list)


def test_the_pipeline_prefers_the_streamed_path():
    """Structural pin: sentence-level buffering is itself a latency floor."""
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._generate_and_speak_response)
    assert "_speak_streamed" in src
    assert src.index("_streaming_enabled") < src.index("_sentence_splitter.split")


def test_the_streamed_path_does_not_gate_the_mic():
    """AEC ROUTING, NOT BINARY GATING. Gating deafened the operator for the
    whole reply — the blindspot, not a smaller version of it."""
    import ast
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._speak_streamed)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree)
    assert "playback_gate" not in code, "the streamed path closes the mic again"
    assert "play_stream" in code, "not routed through the bus — AEC gets no reference"


def test_the_streamed_path_flushes_queued_audio_on_barge_in():
    """Committed audio would keep playing past the interrupt."""
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._speak_streamed)
    assert "flush_playback" in src
