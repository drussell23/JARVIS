"""Unit tests for the AdaptiveAudioPreprocessor (Slice 250.2b Phase 2).

TDD spine for the adaptive feature-extraction matrix: deterministic pure-numpy
pad/truncate-to-target-length + RMS normalization, plus the structural
``RawAudioSource`` injection contract (Phase 3) that aligns to the 250.2a
AudioCapture seam WITHOUT importing it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tests.ml.adaptive_audio_preprocessor import (
    AdaptiveAudioPreprocessor,
    PreprocessConfig,
    RawAudioSource,
)

SR = 16000


# --------------------------------------------------------------------------- #
# Padding (input shorter than target)
# --------------------------------------------------------------------------- #
def test_pad_center_reaches_target_and_preserves_original() -> None:
    target = 1000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, pad="center")
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.linspace(0.2, 0.8, 400, dtype=np.float32)  # shorter than target
    out = pre.fit_length(x)

    assert out.shape == (target,)
    # center placement: 600 pad split 300 left / 300 right.
    left = (target - x.size) // 2
    assert np.allclose(out[left : left + x.size], x)
    # Pad regions are exactly pad_value (0.0 default).
    assert np.all(out[:left] == cfg.pad_value)
    assert np.all(out[left + x.size :] == cfg.pad_value)


def test_pad_tail_appends_pad_value_after_signal() -> None:
    target = 1000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, pad="tail")
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.full(300, 0.5, dtype=np.float32)
    out = pre.fit_length(x)

    assert out.shape == (target,)
    assert np.allclose(out[: x.size], x)
    assert np.all(out[x.size :] == cfg.pad_value)


# --------------------------------------------------------------------------- #
# Truncation (input longer than target)
# --------------------------------------------------------------------------- #
def test_truncate_center_window_preserved() -> None:
    target = 500
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, truncate="center")
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.arange(1200, dtype=np.float32)
    out = pre.fit_length(x)

    assert out.shape == (target,)
    start = (x.size - target) // 2
    assert np.allclose(out, x[start : start + target])


def test_truncate_head_keeps_leading_window() -> None:
    target = 500
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, truncate="head")
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.arange(1200, dtype=np.float32)
    out = pre.fit_length(x)

    assert out.shape == (target,)
    assert np.allclose(out, x[:target])


# --------------------------------------------------------------------------- #
# Exact-length input
# --------------------------------------------------------------------------- #
def test_exact_length_unchanged_by_fit_length() -> None:
    target = 800
    cfg = PreprocessConfig(target_length=target, sample_rate=SR)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.linspace(-0.5, 0.5, target, dtype=np.float32)
    out = pre.fit_length(x)

    assert out.shape == (target,)
    assert np.allclose(out, x)


def test_exact_length_preprocess_preserves_content_modulo_rms() -> None:
    target = 800
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.sin(np.linspace(0, 20 * np.pi, target)).astype(np.float32) * 0.7
    out = pre.preprocess(x)

    assert out.shape == (target,)
    # Same waveform shape up to a positive scalar => correlation ~ 1.
    corr = float(np.corrcoef(out.astype(np.float64), x.astype(np.float64))[0, 1])
    assert corr > 0.999


# --------------------------------------------------------------------------- #
# RMS normalization
# --------------------------------------------------------------------------- #
def test_rms_normalize_hits_target() -> None:
    target = 4000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.sin(np.linspace(0, 50 * np.pi, target)).astype(np.float32) * 0.02  # quiet
    out = pre.rms_normalize(x)

    rms = math.sqrt(float(np.mean(out.astype(np.float64) ** 2)))
    assert abs(rms - cfg.target_rms) < 1e-3


def test_rms_normalize_silence_returned_unchanged_no_nan() -> None:
    target = 1000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.zeros(target, dtype=np.float32)
    out = pre.rms_normalize(x)

    assert np.array_equal(out, x)
    assert not np.any(np.isnan(out))
    assert not np.any(np.isinf(out))


def test_preprocess_silence_no_nan_inf() -> None:
    target = 1000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR)
    pre = AdaptiveAudioPreprocessor(cfg)

    out = pre.preprocess(np.zeros(target, dtype=np.float32))
    assert out.shape == (target,)
    assert not np.any(np.isnan(out))
    assert not np.any(np.isinf(out))


def test_constant_dc_input_handled() -> None:
    target = 1000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.full(target, 0.3, dtype=np.float32)  # nonzero RMS DC
    out = pre.rms_normalize(x)
    rms = math.sqrt(float(np.mean(out.astype(np.float64) ** 2)))
    assert abs(rms - cfg.target_rms) < 1e-3
    assert not np.any(np.isnan(out))


def test_preprocess_output_within_clip_bounds() -> None:
    target = 2000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR, target_rms=0.1)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.sin(np.linspace(0, 30 * np.pi, target)).astype(np.float32)
    out = pre.preprocess(x)
    assert float(np.max(np.abs(out))) <= 1.0 + 1e-6


# --------------------------------------------------------------------------- #
# Determinism + dtype
# --------------------------------------------------------------------------- #
def test_preprocess_deterministic_byte_identical() -> None:
    target = 3000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR)
    pre = AdaptiveAudioPreprocessor(cfg)

    x = np.sin(np.linspace(0, 40 * np.pi, 1500)).astype(np.float32) * 0.5
    out1 = pre.preprocess(x)
    out2 = pre.preprocess(x)

    assert out1.dtype == np.float32
    assert out1.tobytes() == out2.tobytes()


def test_preprocess_output_dtype_and_shape() -> None:
    target = 1234
    cfg = PreprocessConfig(target_length=target, sample_rate=SR)
    pre = AdaptiveAudioPreprocessor(cfg)
    out = pre.preprocess(np.ones(500, dtype=np.float64))
    assert out.dtype == np.float32
    assert out.shape == (target,)


# --------------------------------------------------------------------------- #
# Config from_env / for_duration
# --------------------------------------------------------------------------- #
def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_AUDIO_PREPROC_TARGET_LENGTH", raising=False)
    monkeypatch.delenv("JARVIS_AUDIO_PREPROC_TARGET_RMS", raising=False)
    monkeypatch.delenv("JARVIS_AUDIO_PREPROC_SAMPLE_RATE", raising=False)
    cfg = PreprocessConfig.from_env()
    assert cfg.sample_rate == 16000
    assert abs(cfg.target_rms - 0.1) < 1e-9
    assert cfg.target_length > 0


def test_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_AUDIO_PREPROC_TARGET_LENGTH", "32000")
    monkeypatch.setenv("JARVIS_AUDIO_PREPROC_TARGET_RMS", "0.25")
    monkeypatch.setenv("JARVIS_AUDIO_PREPROC_SAMPLE_RATE", "8000")
    cfg = PreprocessConfig.from_env()
    assert cfg.target_length == 32000
    assert abs(cfg.target_rms - 0.25) < 1e-9
    assert cfg.sample_rate == 8000


def test_for_duration_helper() -> None:
    cfg = PreprocessConfig.for_duration(2.0, sample_rate=16000)
    assert cfg.target_length == 32000
    assert cfg.sample_rate == 16000


# --------------------------------------------------------------------------- #
# Structural Protocol (Phase 3 injection contract)
# --------------------------------------------------------------------------- #
class _MockSource:
    """Structurally aligned to the 250.2a AudioCapture seam (no import)."""

    def __init__(self, samples: np.ndarray, sample_rate: int = SR) -> None:
        self._samples = np.asarray(samples, dtype=np.float32)
        self.sample_rate = sample_rate

    def read(self) -> np.ndarray:
        return self._samples


def test_mock_source_is_instance_of_protocol() -> None:
    src = _MockSource(np.zeros(100, dtype=np.float32))
    assert isinstance(src, RawAudioSource)


def test_object_missing_read_is_not_protocol_instance() -> None:
    class _NoRead:
        sample_rate = SR

    assert not isinstance(_NoRead(), RawAudioSource)


@pytest.mark.asyncio
async def test_preprocess_from_source_consumes_protocol() -> None:
    target = 2000
    cfg = PreprocessConfig(target_length=target, sample_rate=SR)
    pre = AdaptiveAudioPreprocessor(cfg)

    raw = np.sin(np.linspace(0, 10 * np.pi, 800)).astype(np.float32) * 0.4
    src = _MockSource(raw)
    out = await pre.preprocess_from_source(src)

    assert out.shape == (target,)
    assert out.dtype == np.float32
    # Same as the direct path on the raw samples.
    direct = pre.preprocess(raw)
    assert out.tobytes() == direct.tobytes()
