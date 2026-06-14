"""Dual-case speaker-parity harness for the synthetic ECAPA fixtures.

Slice 250.2b Phase 1. Proves the fixtures are genuinely *discriminative*:
B (same voice as A, different utterance) ACCEPTS, and C (a different voice)
REJECTS, under a deterministic pure-numpy spectral embedder. The same parity
logic is structured to run against the real ECAPA embedder when importable
(skips in sandbox where torch/model/cloud are unavailable).
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tests.ml.synthetic_audio_matrix import SAMPLE_RATE, build_abc_matrix

EmbedFn = Callable[[np.ndarray, int], np.ndarray]


# --------------------------------------------------------------------------- #
# Deterministic pure-numpy spectral stand-in embedder
# --------------------------------------------------------------------------- #
def _spectral_embedding(
    waveform: np.ndarray,
    sample_rate: int,
    *,
    frame: int = 512,
    hop: int = 256,
) -> np.ndarray:
    """Hann-windowed framed log-magnitude FFT, mean over frames, L2-normalized.

    Captures the f0 + formant envelope so same-voice clips embed close and
    different-voice clips embed far. Deterministic and dependency-free.
    """
    wav = np.asarray(waveform, dtype=np.float64)
    if wav.size < frame:
        wav = np.pad(wav, (0, frame - wav.size))
    window = np.hanning(frame)
    n_frames = 1 + (wav.size - frame) // hop
    acc = np.zeros(frame // 2 + 1, dtype=np.float64)
    for i in range(n_frames):
        seg = wav[i * hop : i * hop + frame] * window
        mag = np.abs(np.fft.rfft(seg))
        acc += np.log1p(mag)
    acc /= max(n_frames, 1)
    norm = np.linalg.norm(acc)
    return acc / norm if norm > 0.0 else acc


def cosine_similarity(u: np.ndarray, v: np.ndarray) -> float:
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    nu = np.linalg.norm(u)
    nv = np.linalg.norm(v)
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return float(np.dot(u, v) / (nu * nv))


def verify(
    reference: np.ndarray,
    probe: np.ndarray,
    *,
    embed_fn: EmbedFn = _spectral_embedding,
    sample_rate: int = SAMPLE_RATE,
    threshold: float,
) -> bool:
    """Return True iff probe is the same speaker as reference (sim >= threshold)."""
    ref_emb = embed_fn(reference, sample_rate)
    probe_emb = embed_fn(probe, sample_rate)
    return cosine_similarity(ref_emb, probe_emb) >= threshold


# --------------------------------------------------------------------------- #
# Embedder seam: spectral stand-in (default) + real ECAPA when available
# --------------------------------------------------------------------------- #
def _real_ecapa_embed_fn() -> EmbedFn | None:
    """Return a usable real-ECAPA embed_fn, or None if unavailable.

    Skips cleanly in sandbox where torch/model/cloud are not present.
    """
    try:
        from backend.core import ecapa_facade  # type: ignore
    except Exception:
        return None

    extractor = getattr(ecapa_facade, "extract_embedding", None)
    if not callable(extractor):
        return None

    def _embed(waveform: np.ndarray, sample_rate: int) -> np.ndarray:
        # The real facade expects 16 kHz mono float32 in [-1, 1].
        return np.asarray(extractor(waveform), dtype=np.float64)

    # Probe once on a tiny clip; if it raises (no model / no cloud), bail.
    try:
        _ = _embed(np.zeros(SAMPLE_RATE, dtype=np.float32), SAMPLE_RATE)
    except Exception:
        return None
    return _embed


@pytest.fixture(
    params=["spectral", "real_ecapa"],
    ids=["spectral_standin", "real_ecapa"],
)
def embed_fn(request: pytest.FixtureRequest) -> EmbedFn:
    if request.param == "spectral":
        return _spectral_embedding
    real = _real_ecapa_embed_fn()
    if real is None:
        pytest.skip("real ECAPA embedder unavailable (torch/model/cloud not usable)")
    return real


# --------------------------------------------------------------------------- #
# Threshold derivation — proves the fixtures separate with margin
# --------------------------------------------------------------------------- #
def _sims(embed: EmbedFn) -> tuple[float, float]:
    m = build_abc_matrix()
    ea = embed(m.a, SAMPLE_RATE)
    eb = embed(m.b, SAMPLE_RATE)
    ec = embed(m.c, SAMPLE_RATE)
    return cosine_similarity(ea, eb), cosine_similarity(ea, ec)


def _accept_threshold(sim_ab: float, sim_ac: float) -> float:
    """Midpoint between the same-voice and diff-voice sims."""
    return (sim_ab + sim_ac) / 2.0


def test_fixtures_separate_with_margin(embed_fn: EmbedFn) -> None:
    sim_ab, sim_ac = _sims(embed_fn)
    # Same voice must score strictly higher than different voice, with margin.
    assert sim_ab > sim_ac
    margin = sim_ab - sim_ac
    assert margin > 0.05, f"insufficient margin {margin:.4f} (AB={sim_ab}, AC={sim_ac})"
    threshold = _accept_threshold(sim_ab, sim_ac)
    assert sim_ac < threshold < sim_ab


def test_b_accepts(embed_fn: EmbedFn) -> None:
    m = build_abc_matrix()
    sim_ab, sim_ac = _sims(embed_fn)
    threshold = _accept_threshold(sim_ab, sim_ac)
    assert sim_ab >= threshold
    assert verify(m.a, m.b, embed_fn=embed_fn, threshold=threshold) is True


def test_c_rejects(embed_fn: EmbedFn) -> None:
    m = build_abc_matrix()
    sim_ab, sim_ac = _sims(embed_fn)
    threshold = _accept_threshold(sim_ab, sim_ac)
    assert sim_ac < threshold
    assert verify(m.a, m.c, embed_fn=embed_fn, threshold=threshold) is False


def test_spectral_threshold_concrete_values() -> None:
    """Pin the spectral-standin numbers so regressions in the fixture contrast
    (f0/formant tuning) are caught explicitly, independent of the param fixture."""
    sim_ab, sim_ac = _sims(_spectral_embedding)
    threshold = _accept_threshold(sim_ab, sim_ac)
    # Same voice ~1.0, different voice well below.
    assert sim_ab > 0.99
    assert sim_ac < 0.6
    assert 0.6 < threshold < 0.99
    assert verify(*_ab_clips(), threshold=threshold) is True
    assert verify(*_ac_clips(), threshold=threshold) is False


def _ab_clips() -> tuple[np.ndarray, np.ndarray]:
    m = build_abc_matrix()
    return m.a, m.b


def _ac_clips() -> tuple[np.ndarray, np.ndarray]:
    m = build_abc_matrix()
    return m.a, m.c
