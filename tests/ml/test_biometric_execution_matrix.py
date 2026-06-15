"""BiometricExecutionMatrix — happy-path + fail-secure unit tests.

Slice 250.2b Phase 3. Proves the synchronous, fail-secure authentication
verdict path: real-fixture ACCEPT/REJECT discrimination (reusing Phase 1/2),
breaker-open short-circuit (embedder never called), and generic-error
fail-secure (REJECTED + record_failure).
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tests.ml.adaptive_audio_preprocessor import (
    AdaptiveAudioPreprocessor,
    PreprocessConfig,
)
from tests.ml.biometric_execution_matrix import (
    AuthResult,
    BiometricExecutionMatrix,
    LocalOOMCircuitBreaker,
    Verdict,
)
from tests.ml.synthetic_audio_matrix import SAMPLE_RATE, build_abc_matrix
from tests.ml.test_speaker_parity_harness import (
    _accept_threshold,
    _spectral_embedding,
    cosine_similarity,
)


# --------------------------------------------------------------------------- #
# Helpers — adapt the (waveform, sample_rate) spectral fn into the single-arg
# Embedder contract Callable[[np.ndarray], np.ndarray].
# --------------------------------------------------------------------------- #
def _single_arg_spectral() -> Callable[[np.ndarray], np.ndarray]:
    return lambda wav: _spectral_embedding(wav, SAMPLE_RATE)


def _real_pipeline() -> tuple[
    Callable[[np.ndarray], np.ndarray], np.ndarray, np.ndarray, np.ndarray, float
]:
    """Build the embedder, baseline (A), B-probe, C-probe, and threshold using
    the Phase 1 fixtures + Phase 2 preprocessor + Phase 1 spectral embedder."""
    m = build_abc_matrix()
    pre = AdaptiveAudioPreprocessor(
        PreprocessConfig.for_duration(3.0, sample_rate=SAMPLE_RATE)
    )
    a = pre.preprocess(m.a)
    b = pre.preprocess(m.b)
    c = pre.preprocess(m.c)
    embed = _single_arg_spectral()
    baseline = embed(a)
    # Threshold derived exactly as the parity harness does, on the same
    # preprocessed clips (midpoint between same-voice and diff-voice sims).
    sim_ab = cosine_similarity(baseline, embed(b))
    sim_ac = cosine_similarity(baseline, embed(c))
    threshold = _accept_threshold(sim_ab, sim_ac)
    return embed, baseline, b, c, threshold


# --------------------------------------------------------------------------- #
# Happy path — real fixtures
# --------------------------------------------------------------------------- #
def test_happy_path_b_accepts_c_rejects() -> None:
    embed, baseline, b, c, threshold = _real_pipeline()
    matrix = BiometricExecutionMatrix(
        embedder=embed,
        baseline_embedding=baseline,
        accept_threshold=threshold,
    )

    res_b = matrix.authenticate(b)
    assert isinstance(res_b, AuthResult)
    assert res_b.verdict is Verdict.ACCEPTED
    assert res_b.score >= threshold
    assert res_b.reason == "evaluated"

    res_c = matrix.authenticate(c)
    assert res_c.verdict is Verdict.REJECTED
    assert res_c.score < threshold
    assert res_c.reason == "evaluated"


# --------------------------------------------------------------------------- #
# Breaker-open fail-secure: embedder is NEVER called.
# --------------------------------------------------------------------------- #
class _OpenBreaker:
    """can_execute() always False — the circuit is open/locked."""

    def __init__(self) -> None:
        self.failures = 0
        self.successes = 0

    def can_execute(self) -> bool:
        return False

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self, error=None) -> None:
        self.failures += 1


class _SpyEmbedder:
    def __init__(self, ret: np.ndarray | None = None) -> None:
        self.calls = 0
        self._ret = ret if ret is not None else np.ones(4, dtype=np.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        self.calls += 1
        return self._ret


def test_breaker_open_fails_secure_without_calling_embedder() -> None:
    spy = _SpyEmbedder()
    matrix = BiometricExecutionMatrix(
        embedder=spy,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=_OpenBreaker(),
    )

    res = matrix.authenticate(np.zeros(16, dtype=np.float64))

    assert res.verdict is Verdict.REJECTED
    assert res.reason == "circuit_open_locked"
    assert res.score == 0.0
    assert spy.calls == 0, "embedder MUST NOT be called when the breaker is open"


# --------------------------------------------------------------------------- #
# Non-OOM (generic) error fail-secure: REJECTED + record_failure.
# --------------------------------------------------------------------------- #
class _CountingBreaker:
    def __init__(self) -> None:
        self.failures: list = []
        self.successes = 0
        self._open = False

    def can_execute(self) -> bool:
        return not self._open

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self, error=None) -> None:
        self.failures.append(error)


def test_generic_error_fails_secure() -> None:
    def _boom(_x: np.ndarray) -> np.ndarray:
        raise ValueError("bad input")

    breaker = _CountingBreaker()
    matrix = BiometricExecutionMatrix(
        embedder=_boom,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=breaker,
    )

    res = matrix.authenticate(np.zeros(16, dtype=np.float64))

    assert res.verdict is Verdict.REJECTED
    assert res.reason == "error_fail_secure"
    assert res.score == 0.0
    assert len(breaker.failures) == 1
    assert isinstance(breaker.failures[0], ValueError)


# --------------------------------------------------------------------------- #
# Default breaker is a standalone LocalOOMCircuitBreaker
# --------------------------------------------------------------------------- #
def test_default_breaker_is_local_oom_breaker() -> None:
    matrix = BiometricExecutionMatrix(
        embedder=_single_arg_spectral(),
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
    )
    assert isinstance(matrix.breaker, LocalOOMCircuitBreaker)
    assert matrix.breaker.can_execute() is True
