"""Integration proof: preprocessing preserves speaker-discriminating envelope.

Slice 250.2b Phase 4. Feeds the Phase 1 A/B/C matrix through the
AdaptiveAudioPreprocessor with NON-UNIFORM input lengths (A padded, B exact,
C truncated), then re-embeds with the Phase 1 spectral embedder and proves the
accept/reject verdicts survive: B still accepts vs A, C still rejects vs A,
with margin > 0.05. This proves pad + RMS-norm do not destroy the spectral
envelope that separates speakers.
"""

from __future__ import annotations

import numpy as np

from tests.ml.adaptive_audio_preprocessor import (
    AdaptiveAudioPreprocessor,
    PreprocessConfig,
)
from tests.ml.synthetic_audio_matrix import SAMPLE_RATE, build_abc_matrix
from tests.ml.test_speaker_parity_harness import (
    _spectral_embedding,
    cosine_similarity,
)

MARGIN_FLOOR = 0.05


def _emb(wav: np.ndarray) -> np.ndarray:
    return _spectral_embedding(wav, SAMPLE_RATE)


def test_parity_preserved_under_nonuniform_preprocessing() -> None:
    m = build_abc_matrix(duration_s=3.0)
    base_len = m.a.size  # 48000 @ 3s/16kHz

    # Target between the short and long variants so we exercise BOTH paths.
    target = base_len
    cfg = PreprocessConfig(
        target_length=target,
        sample_rate=SAMPLE_RATE,
        target_rms=0.1,
        pad="center",
        truncate="center",
    )
    pre = AdaptiveAudioPreprocessor(cfg)

    # Non-uniform inputs: A SHORTER than target (=> padded), B EXACT, C LONGER
    # than target (=> truncated). Slicing keeps content deterministic.
    a_short = m.a[: base_len - 8000]  # 40000 samples -> padded up to 48000
    b_exact = m.b  # 48000 -> unchanged length
    c_long = np.concatenate([m.c, m.c[:8000]])  # 56000 -> truncated to 48000

    assert a_short.size < target < c_long.size
    assert b_exact.size == target

    pa = pre.preprocess(a_short)
    pb = pre.preprocess(b_exact)
    pc = pre.preprocess(c_long)

    assert pa.shape == pb.shape == pc.shape == (target,)

    # Post-preprocessing sims.
    ea, eb, ec = _emb(pa), _emb(pb), _emb(pc)
    sim_ab_post = cosine_similarity(ea, eb)
    sim_ac_post = cosine_similarity(ea, ec)

    # Pre-preprocessing reference sims (raw fixtures, same lengths as Phase 1).
    sim_ab_pre = cosine_similarity(_emb(m.a), _emb(m.b))
    sim_ac_pre = cosine_similarity(_emb(m.a), _emb(m.c))

    # Threshold = midpoint of the PRE sims (the Phase 1 separation contract).
    threshold = (sim_ab_pre + sim_ac_pre) / 2.0

    # Verdicts unchanged: B accepts, C rejects -- both pre and post.
    assert sim_ab_pre >= threshold and sim_ac_pre < threshold  # sanity: pre
    assert sim_ab_post >= threshold, (
        f"B no longer accepts post-preprocess: {sim_ab_post:.4f} < {threshold:.4f}"
    )
    assert sim_ac_post < threshold, (
        f"C no longer rejects post-preprocess: {sim_ac_post:.4f} >= {threshold:.4f}"
    )

    # Margin survives with comfortable headroom.
    margin_post = sim_ab_post - sim_ac_post
    assert margin_post > MARGIN_FLOOR, f"margin collapsed: {margin_post:.4f}"

    # Same accept/reject verdicts pre vs post (the load-bearing invariant).
    assert (sim_ab_pre >= threshold) == (sim_ab_post >= threshold)
    assert (sim_ac_pre < threshold) == (sim_ac_post < threshold)
