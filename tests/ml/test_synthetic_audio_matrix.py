"""Determinism + structure tests for the synthetic ECAPA audio fixture matrix.

Slice 250.2b Phase 1. Pure numpy. The cardinal invariant is *byte-identical
determinism*: identical args MUST produce identical bytes, in-process and
across separate processes (proven separately via sha256 in the verify step).
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.ml.synthetic_audio_matrix import (
    ALPHA,
    BRAVO,
    MAX_MATRIX_BYTES,
    SAMPLE_RATE,
    VoiceProfile,
    build_abc_matrix,
    estimate_matrix_bytes,
    generate_waveform,
)


# --------------------------------------------------------------------------- #
# Byte-identical determinism
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("profile", [ALPHA, BRAVO])
@pytest.mark.parametrize("seed", [7, 0, 42, 101, 202, 303])
def test_byte_identical_same_args(profile: VoiceProfile, seed: int) -> None:
    a = generate_waveform(profile, seed=seed)
    b = generate_waveform(profile, seed=seed)
    assert a.dtype == np.float32
    assert b.dtype == np.float32
    assert a.shape == (int(round(3.0 * SAMPLE_RATE)),)
    assert b.shape == a.shape
    assert np.array_equal(a, b)
    assert a.tobytes() == b.tobytes()


def test_range_within_unit_interval() -> None:
    for profile in (ALPHA, BRAVO):
        wf = generate_waveform(profile, seed=7)
        assert wf.min() >= -1.0
        assert wf.max() <= 1.0
        # peak-normalized to 0.95 -> the magnitude should reach ~0.95
        assert abs(np.abs(wf).max() - 0.95) < 1e-5


def test_duration_and_sample_rate_shape() -> None:
    wf = generate_waveform(ALPHA, seed=7, duration_s=1.5)
    assert wf.shape == (int(round(1.5 * SAMPLE_RATE)),)
    wf2 = generate_waveform(ALPHA, seed=7, duration_s=2.0, sample_rate=8000)
    assert wf2.shape == (int(round(2.0 * 8000)),)


def test_no_global_state_leakage_interleaved() -> None:
    """Interleaving generations of different profiles/seeds must not perturb
    any subsequent generation. Proves we never touch global RNG state."""
    ref_alpha = generate_waveform(ALPHA, seed=7)
    ref_bravo = generate_waveform(BRAVO, seed=11)

    # Interleave a bunch of unrelated generations.
    for s in (1, 2, 3, 999, 12345):
        _ = generate_waveform(ALPHA, seed=s)
        _ = generate_waveform(BRAVO, seed=s)

    again_alpha = generate_waveform(ALPHA, seed=7)
    again_bravo = generate_waveform(BRAVO, seed=11)

    assert ref_alpha.tobytes() == again_alpha.tobytes()
    assert ref_bravo.tobytes() == again_bravo.tobytes()


def test_different_seeds_differ() -> None:
    a = generate_waveform(ALPHA, seed=7)
    b = generate_waveform(ALPHA, seed=8)
    assert a.tobytes() != b.tobytes()


def test_different_profiles_differ() -> None:
    a = generate_waveform(ALPHA, seed=7)
    b = generate_waveform(BRAVO, seed=7)
    assert a.tobytes() != b.tobytes()


# --------------------------------------------------------------------------- #
# ABC matrix structure
# --------------------------------------------------------------------------- #
def test_abc_matrix_structure() -> None:
    m = build_abc_matrix()
    # A & B share profile ALPHA; C is BRAVO.
    assert m.profile_a == ALPHA
    assert m.profile_b == ALPHA
    assert m.profile_c == BRAVO
    # Same voice, different utterance (different seed) -> different bytes.
    assert m.a.tobytes() != m.b.tobytes()
    # Different voice.
    assert m.a.tobytes() != m.c.tobytes()
    # dtype / shape invariants
    n = int(round(3.0 * SAMPLE_RATE))
    for clip in (m.a, m.b, m.c):
        assert clip.dtype == np.float32
        assert clip.shape == (n,)
        assert clip.min() >= -1.0
        assert clip.max() <= 1.0
    # seeds recorded
    assert m.seed_a == 101
    assert m.seed_b == 202
    assert m.seed_c == 303


def test_abc_matrix_deterministic() -> None:
    m1 = build_abc_matrix()
    m2 = build_abc_matrix()
    assert m1.a.tobytes() == m2.a.tobytes()
    assert m1.b.tobytes() == m2.b.tobytes()
    assert m1.c.tobytes() == m2.c.tobytes()


# --------------------------------------------------------------------------- #
# Memory bound (unified-memory / Apple-Silicon 16 GB ceiling)
# --------------------------------------------------------------------------- #
def test_memory_bound() -> None:
    est = estimate_matrix_bytes(3.0)
    assert est > 0
    assert est < MAX_MATRIX_BYTES
    assert est < 16 * 1024**3  # far under the 16 GB RAM ceiling
    # sanity: 3 clips * 3s * 16000 * 4 bytes ~= 576 KB
    assert est == 3 * int(round(3.0 * SAMPLE_RATE)) * 4
