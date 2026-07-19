"""Ears-Ajar Gate spine — pre-roll stitching + adaptive noise floor.

Mandate 4 verbatim (2026-07-19): a wake word whose acoustic envelope
begins 200ms BEFORE the RMS breach must arrive at the recognizer FSM
complete — the pre-roll stitch prepends the missing 200ms.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.core.ouroboros.governance.comms.duplex.ears_ajar import (
    EarsAjarGate,
)

RATE, CHUNK = 16000, 480          # 30ms chunks
CHUNK_S = CHUNK / RATE


def _tone(freq: float, amp: float, chunks: int = 1) -> list:
    t = np.arange(chunks * CHUNK) / RATE
    x = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return [x[i * CHUNK:(i + 1) * CHUNK] for i in range(chunks)]


def _noise(amp: float, chunks: int = 1, seed: int = 7) -> list:
    rng = np.random.default_rng(seed)
    return [
        (amp * rng.standard_normal(CHUNK)).astype(np.float32)
        for _ in range(chunks)
    ]


@pytest.fixture()
def gate(monkeypatch):
    for k in (
        "JARVIS_SENTRY_PREROLL_MS", "JARVIS_SENTRY_FLOOR_ALPHA",
        "JARVIS_SENTRY_ABS_MIN_RMS", "JARVIS_SENTRY_FLOOR_MULT",
    ):
        monkeypatch.delenv(k, raising=False)
    return EarsAjarGate(rate=RATE, chunk=CHUNK)


class TestPreRollStitch:
    def test_wake_word_starting_200ms_before_breach_is_prepended(self, gate):
        """MANDATE 4 VERBATIM. Timeline:
        quiet noise → 200ms of SOFT voice-band onset (below threshold —
        the leading consonant) → LOUD voice-band chunk (breach). The
        stitched payload must contain the soft 200ms onset."""
        for c in _noise(0.001, chunks=30):        # settle the floor
            assert gate.feed(c) is None
        # The wake word's soft onset: 200ms ≈ 7 chunks at 300Hz, quiet
        # enough to stay under 3×floor... use amp below abs_min too.
        onset = _tone(300.0, 0.004, chunks=7)     # sub-threshold voice
        for c in onset:
            assert gate.feed(c) is None            # gate stays cold
        loud = _tone(300.0, 0.2, chunks=1)[0]      # the stressed vowel
        payload = gate.feed(loud)
        assert payload is not None                 # gate fired
        # The payload ends with the breach chunk...
        assert np.array_equal(payload[-CHUNK:], loud)
        # ...and STRUCTURALLY CONTAINS the full 200ms onset immediately
        # before it (byte-identical samples — nothing truncated).
        onset_cat = np.concatenate(onset)
        n = onset_cat.size
        assert np.array_equal(payload[-CHUNK - n:-CHUNK], onset_cat)
        # Total pre-roll ≈ 500ms: payload spans breach + ring.
        assert payload.size == CHUNK * (gate.preroll_chunks + 1)

    def test_first_chunk_breach_without_history_still_fires(self, gate):
        loud = _tone(300.0, 0.3, chunks=1)[0]
        payload = gate.feed(loud)
        assert payload is not None
        assert np.array_equal(payload, loud)       # nothing to stitch yet

    def test_ring_is_bounded_fifo(self, gate):
        for c in _noise(0.001, chunks=200):        # 6s >> 500ms ring
            gate.feed(c)
        assert len(gate._preroll) == gate.preroll_chunks
        assert gate.preroll_chunks == round(0.5 * RATE / CHUNK)  # ~500ms

    def test_open_window_streams_raw_not_restitched(self, gate):
        for c in _noise(0.001, chunks=30):
            gate.feed(c)
        assert gate.feed(_tone(300.0, 0.3)[0]) is not None
        assert gate.in_window is True
        # While the window is open, feed returns None (caller forwards
        # the live stream); ring keeps rolling for the NEXT trigger.
        assert gate.feed(_tone(300.0, 0.3)[0]) is None
        gate.close_window()
        assert gate.in_window is False


class TestDynamicNoiseFloor:
    def test_hvac_engagement_raises_floor_no_runaway_triggers(
        self, gate, monkeypatch,
    ):
        for c in _noise(0.001, chunks=50):
            gate.feed(c)
        floor_before = gate.noise_floor
        # HVAC engages: sustained broadband noise well above the old
        # floor. First moments may trigger (legitimately loud), but the
        # floor absorbs it and triggers STOP.
        fan = _noise(0.02, chunks=1200, seed=11)   # ~36s of fan
        triggers = 0
        for c in fan:
            if gate.feed(c) is not None:
                triggers += 1
                gate.close_window()
        assert gate.noise_floor > floor_before * 4  # recalibrated
        # Runaway = triggering on a large fraction of fan chunks.
        assert triggers < 60                        # <5% of 1200
        # Late fan chunks are absorbed silently:
        late = 0
        for c in _noise(0.02, chunks=100, seed=12):
            if gate.feed(c) is not None:
                late += 1
                gate.close_window()
        assert late == 0                            # fully adapted

    def test_floor_drift_telemetry_recorded(self, gate):
        for c in _noise(0.001, chunks=20):
            gate.feed(c)
        for c in _noise(0.05, chunks=200, seed=3):
            gate.feed(c)
            if gate.in_window:
                gate.close_window()
        assert gate.stats["floor_max"] > gate.stats["floor_min"]
        assert gate.stats["chunks"] == 220

    def test_non_voice_broadband_transient_rejected(self, gate):
        """A door slam: loud but spectrally flat → voice-ratio veto."""
        for c in _noise(0.001, chunks=30):
            gate.feed(c)
        slam = _noise(0.4, chunks=1, seed=99)[0]   # white = flat spectrum
        assert gate.feed(slam) is None

    def test_hostile_chunk_never_raises(self, gate):
        assert gate.feed(np.array([])) is None
        assert gate.feed("not audio") is None      # type: ignore[arg-type]
