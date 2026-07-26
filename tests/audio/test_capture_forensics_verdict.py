"""Capture-forensics verdict — window-scoped evidence only.

The recorder shipped (#70096) without a spine over ``_verdict``, and the gap
cost real diagnostic time: the ring's LIFETIME over-full-scale counter was the
verdict's opening branch, so one overdriven frame early in a process latched
"input gain, not the chain" onto every rejection written afterwards. The
modulation comparison the hint exists for never ran again, and the false
attribution contradicted the measured cause (mic distance / envelope smearing).

These tests pin the two invariants that failure violated:

  1. lifetime totals are never read as incident evidence, and
  2. overdrive is a MECHANISM offered alongside measured chain damage, never
     a standalone cause — ``AudioBus._fit_to_range`` compensates it by design.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.audio.capture_forensics import CaptureForensics, _Ring


def _speechlike(seconds: float, rate: int, *, amp: float = 0.2) -> np.ndarray:
    """A carrier amplitude-modulated at 4 Hz — inside the 2-8 Hz syllabic
    band the verdict reads, so it registers as 'a voice was present'."""
    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    envelope = 0.5 * (1.0 + np.sin(2.0 * np.pi * 4.0 * t))
    return (amp * envelope * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)


def _unique(seconds: float, rate: int, *, seed: int = 7) -> np.ndarray:
    """Speech-shaped but NON-REPEATING — a syllabic envelope over seeded
    noise. ``_speechlike`` is exactly periodic (a 220 Hz carrier under a 4 Hz
    envelope repeats every 0.25 s), which makes it correlate perfectly at many
    lags; any alignment measured against it is arbitrary. Content that has to
    be *located* needs a signal that occurs only once."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    envelope = 0.5 * (1.0 + np.sin(2.0 * np.pi * 4.0 * t))
    return (0.2 * envelope * rng.standard_normal(t.size)).astype(np.float32)


def _steady(seconds: float, rate: int, *, amp: float = 0.2) -> np.ndarray:
    """A constant tone: energy without syllabic rhythm."""
    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    return (amp * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)


def _stats(frames: list[np.ndarray], rate: int, window_s: float = 12.0) -> dict:
    """Push *frames* through a ring in realistic 100 ms chunks and read it.

    Chunking matters: the ring evicts whole frames, so a single oversized
    push would evict everything before it and leave the window empty."""
    ring = _Ring(rate, window_s)
    hop = max(1, rate // 10)
    for f in frames:
        for start in range(0, len(f), hop):
            ring.push(f[start:start + hop])
    return ring.stats()


# --------------------------------------------------------------------------
# 1. scope: lifetime totals stay out of the window block
# --------------------------------------------------------------------------

def test_lifetime_totals_are_quarantined_under_session() -> None:
    rate = 16000
    ring = _Ring(rate, 12.0)
    ring.push(np.array([5.8, -5.8], dtype=np.float32))   # the early transient
    for _ in range(20):
        ring.push(_speechlike(0.5, rate))                # ...long since evicted

    stats = ring.stats()
    assert "over_full_scale_frames" not in stats
    assert "peak_ever" not in stats
    assert "frames_seen" not in stats
    assert stats["session"]["over_full_scale_frames"] == 1
    assert stats["session"]["peak_ever"] == pytest.approx(5.8, abs=1e-3)


def test_window_overdrive_counts_only_retained_audio() -> None:
    rate = 16000
    ring = _Ring(rate, 1.0)                     # 1s budget
    ring.push(np.full(rate, 3.0, dtype=np.float32))      # 1s, overdriven
    ring.push(_speechlike(1.0, rate))                    # evicts the above

    stats = ring.stats()
    assert stats["over_full_scale_samples"] == 0         # window is clean
    assert stats["session"]["over_full_scale_frames"] == 1   # history remembers


# --------------------------------------------------------------------------
# 2. the latch: a past transient must not decide a present incident
# --------------------------------------------------------------------------

def test_past_overdrive_does_not_latch_the_verdict() -> None:
    """The regression itself. Raw and processed both carry speech rhythm, and
    the only overdrive is historical — the verdict must reason about the
    chain, not recite the device's past."""
    rate = 16000
    # A 1s window with 3s of speech pushed after the transient: the
    # overdriven frame is long gone from the retained audio.
    raw = _stats(
        [np.array([5.8], dtype=np.float32), _speechlike(3.0, rate)], rate, 1.0,
    )
    proc = _stats([_speechlike(3.0, rate)], rate, 1.0)
    assert raw["session"]["over_full_scale_frames"] == 1
    assert raw["over_full_scale_samples"] == 0

    verdict = CaptureForensics._verdict(
        {"raw_device": raw, "processed_bus": proc}
    )
    assert "input gain, not the chain" not in verdict
    assert "survives the chain" in verdict


def test_silent_mic_is_attributed_to_the_microphone() -> None:
    """The measured cause of the 2026-07-25 session: rhythm absent at the
    device end. A stale overdrive counter must not steal this verdict."""
    rate = 16000
    raw = _stats([np.array([5.8], dtype=np.float32), _steady(3.0, rate)], rate)
    proc = _stats([_steady(3.0, rate)], rate)

    verdict = CaptureForensics._verdict(
        {"raw_device": raw, "processed_bus": proc}
    )
    assert "did not hear a voice" in verdict
    assert "input gain" not in verdict


# --------------------------------------------------------------------------
# 3. overdrive as mechanism, not cause
# --------------------------------------------------------------------------

def test_live_overdrive_is_reported_only_with_measured_chain_damage() -> None:
    rate = 16000
    raw = _stats([_speechlike(3.0, rate, amp=3.0)], rate)   # overdriven NOW
    proc = _stats([_steady(3.0, rate)], rate)               # rhythm destroyed
    assert raw["over_full_scale_samples"] > 0

    verdict = CaptureForensics._verdict(
        {"raw_device": raw, "processed_bus": proc}
    )
    assert "the chain is destroying it" in verdict
    assert "above full scale" in verdict


def test_overdrive_alone_is_not_a_fault() -> None:
    """_fit_to_range compensates overdrive by design. Present at both ends
    with rhythm intact, it is not a finding."""
    rate = 16000
    raw = _stats([_speechlike(3.0, rate, amp=3.0)], rate)
    proc = _stats([_speechlike(3.0, rate)], rate)
    assert raw["over_full_scale_samples"] > 0

    verdict = CaptureForensics._verdict(
        {"raw_device": raw, "processed_bus": proc}
    )
    assert "survives the chain" in verdict


# --------------------------------------------------------------------------
# 4. degenerate inputs keep their honest answers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "metrics, expected",
    [
        ({"raw_device": {"samples": 0}, "processed_bus": {}},
         "no raw frames tapped"),
        ({"raw_device": {"samples": 10}, "processed_bus": {}},
         "incomplete measurements"),
    ],
)
def test_degenerate_inputs(metrics: dict, expected: str) -> None:
    assert expected in CaptureForensics._verdict(metrics)


def test_empty_ring_stats_still_carry_session_totals() -> None:
    ring = _Ring(16000, 12.0)
    stats = ring.stats()
    assert stats["samples"] == 0
    assert stats["session"]["frames_seen"] == 0


# --------------------------------------------------------------------------
# 5. handoff integrity — is the model buffer the audio the bus produced?
# --------------------------------------------------------------------------

def _fill(fx: CaptureForensics, audio: np.ndarray, rate: int) -> None:
    hop = max(1, rate // 10)
    for s in range(0, len(audio), hop):
        fx.note_processed(audio[s:s + hop], rate)


def test_handoff_lossless_is_proven_not_assumed() -> None:
    """The measured production case: the recogniser is handed a slice of the
    bus audio, several seconds back from the newest sample, bit-exact."""
    rate = 16000
    bus = _unique(10.0, rate)
    fx = CaptureForensics()
    _fill(fx, bus, rate)

    # An utterance 4s back from the end — where transcription latency puts it.
    start, n = int(3.0 * rate), int(2.0 * rate)
    handed = bus[start:start + n]

    report = fx._handoff(handed, rate)
    assert report["status"] == "lossless"
    assert report["correlation"] == pytest.approx(1.0, abs=1e-3)
    assert report["gain_db"] == pytest.approx(0.0, abs=0.05)
    assert report["lag_s"] == pytest.approx(10.0 - 5.0, abs=0.05)


@pytest.mark.parametrize(
    "gain, label",
    [
        (1.0 / 32768.0, "unscaled int16->float32 cast"),
        (0.5, "one bit-shift"),
        (2.0, "double normalisation"),
    ],
)
def test_handoff_detects_a_rescaled_buffer(gain: float, label: str) -> None:
    """The fault class hypothesised for this session. It did not turn out to
    be present, and the point of the check is that if it ever is, the
    recorder names it on the first incident instead of costing a hunt."""
    rate = 16000
    bus = _unique(8.0, rate)
    fx = CaptureForensics()
    _fill(fx, bus, rate)

    handed = bus[int(2.0 * rate):int(4.0 * rate)] * gain

    report = fx._handoff(handed, rate)
    assert report["status"] == "scaled", label
    assert report["correlation"] == pytest.approx(1.0, abs=1e-3)
    assert report["gain_db"] == pytest.approx(
        20.0 * np.log10(gain), abs=0.1,
    )

    verdict = CaptureForensics._verdict({
        "raw_device": {"samples": 1, "syllabic_modulation_2_8hz": 0.3},
        "processed_bus": {"syllabic_modulation_2_8hz": 0.3},
        "handoff": report,
    })
    assert "the handoff is rescaling it" in verdict


def test_unproven_alignment_never_claims_divergence() -> None:
    """Epistemic floor. Audio that aged out of the ring must read
    'unverifiable', never 'the buffer diverged' — an absent match is not
    evidence of a fault."""
    rate = 16000
    fx = CaptureForensics()
    _fill(fx, _unique(4.0, rate), rate)

    unrelated = _steady(1.0, rate) + 0.01 * np.arange(rate, dtype=np.float32)
    report = fx._handoff(unrelated.astype(np.float32), rate)
    assert report["status"] == "unverifiable"

    verdict = CaptureForensics._verdict({
        "raw_device": {"samples": 1, "syllabic_modulation_2_8hz": 0.3},
        "processed_bus": {"syllabic_modulation_2_8hz": 0.3},
        "handoff": report,
    })
    assert "rescaling" not in verdict


def test_resampled_handoff_is_unverifiable_not_a_fault() -> None:
    rate = 16000
    fx = CaptureForensics()
    _fill(fx, _unique(4.0, rate), rate)
    report = fx._handoff(_unique(1.0, 8000), 8000)
    assert report["status"] == "unverifiable"
    assert report["why"] == "rate differs"


def test_handoff_check_is_killable() -> None:
    import os
    rate = 16000
    fx = CaptureForensics()
    _fill(fx, _unique(4.0, rate), rate)
    os.environ["JARVIS_FORENSICS_HANDOFF_CHECK"] = "0"
    try:
        assert fx._handoff(_unique(1.0, rate), rate)["status"] == "disabled"
    finally:
        os.environ.pop("JARVIS_FORENSICS_HANDOFF_CHECK", None)


def test_lossless_handoff_is_stated_in_the_verdict() -> None:
    verdict = CaptureForensics._verdict({
        "raw_device": {"samples": 1, "syllabic_modulation_2_8hz": 0.25},
        "processed_bus": {"syllabic_modulation_2_8hz": 0.26},
        "handoff": {"status": "lossless", "correlation": 1.0, "gain_db": 0.0},
    })
    assert "handed the bus audio unchanged" in verdict
    assert "in the recogniser or in what was buffered for it" in verdict
