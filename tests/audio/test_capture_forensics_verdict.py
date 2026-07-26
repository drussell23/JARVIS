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
