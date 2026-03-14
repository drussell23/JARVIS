# Adaptive Quantization Engine Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an intelligent, adaptive model quantization management system for jarvis-prime that dynamically selects optimal models based on VRAM, task complexity, and quantization quality metrics.

**Architecture:** Advisory Layer (4 pure modules: QuantizationIntelligence, KVCacheOptimizer, AdaptiveModelSelector, VRAMPressureMonitor) feeds a single serialized executor (ModelTransitionManager) which coordinates with VRAMBudgetAuthority, LlamaCppExecutor, and GCPModelSwapCoordinator. QualityRegressionTester runs background calibration.

**Tech Stack:** Python 3.10+, asyncio, pytest + pytest-asyncio (asyncio_mode=auto), pynvml (optional), dataclasses, pathlib

**Spec:** `docs/superpowers/specs/2026-03-14-adaptive-quantization-engine-design.md`

**Target repo:** `/Users/djrussell23/Documents/repos/jarvis-prime/`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `jarvis_prime/core/quantization_intelligence.py` | Rate-distortion scoring engine (~400 lines) |
| Create | `jarvis_prime/core/kv_cache_optimizer.py` | KV cache math & context window maximizer (~250 lines) |
| Create | `jarvis_prime/core/adaptive_model_selector.py` | Inventory scanning & proposal engine (~600 lines) |
| Create | `jarvis_prime/core/model_transition_manager.py` | FSM executor + VRAMBudgetAuthority (~700 lines) |
| Create | `jarvis_prime/core/vram_pressure_monitor.py` | GPU memory watchdog (~350 lines) |
| Create | `jarvis_prime/core/quality_regression_tester.py` | A/B benchmarking & calibration (~300 lines) |
| Create | `tests/test_quantization_intelligence.py` | Tests for scoring engine |
| Create | `tests/test_kv_cache_optimizer.py` | Tests for KV cache math |
| Create | `tests/test_adaptive_model_selector.py` | Tests for inventory & proposals |
| Create | `tests/test_model_transition_manager.py` | Tests for FSM + VRAMBudgetAuthority |
| Create | `tests/test_vram_pressure_monitor.py` | Tests for pressure monitor |
| Create | `tests/test_quality_regression_tester.py` | Tests for benchmarking |
| Create | `tests/conftest.py` | Shared fixtures for all tests |
| Modify | `jarvis_prime/server.py:1562-1650` | Extend `/v1/capability` with quantization + epoch data |
| Modify | `run_server.py` | Wire AdaptiveModelSelector at startup |

---

## Chunk 1: Test Infrastructure + Quantization Intelligence

### Task 1: Test Infrastructure (conftest.py)

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Create shared test fixtures**

```python
"""Shared fixtures for adaptive quantization engine tests."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def tmp_models_dir(tmp_path: Path) -> Path:
    """Create a temporary models directory with fake GGUF files."""
    models = tmp_path / "models"
    models.mkdir()
    return models


@pytest.fixture
def fake_gguf_files(tmp_models_dir: Path) -> Dict[str, Path]:
    """Create fake GGUF files of known sizes for testing."""
    files = {}
    specs = {
        "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf": 4_400_000_000,
        "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf": 8_400_000_000,
        "Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf": 11_000_000_000,
        "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf": 19_000_000_000,
        "Llama-3.2-1B-Instruct-Q4_K_M.gguf": 771_000_000,
    }
    for name, size in specs.items():
        p = tmp_models_dir / name
        # Create sparse file (doesn't use disk space)
        with open(p, "wb") as f:
            f.seek(size - 1)
            f.write(b"\0")
        files[name] = p
    return files


@pytest.fixture
def l4_vram_bytes() -> int:
    """NVIDIA L4 total VRAM in bytes."""
    return 23_034 * 1024 * 1024  # 23,034 MiB


@pytest.fixture
def mock_executor() -> MagicMock:
    """Mock LlamaCppExecutor for transition tests."""
    executor = MagicMock()
    executor.load = AsyncMock()
    executor.unload = AsyncMock()
    executor.validate = AsyncMock(return_value=True)
    executor.generate = AsyncMock(return_value="test response")
    executor.is_loaded = MagicMock(return_value=True)
    executor._model_path = None
    executor.config = MagicMock()
    executor.config.n_gpu_layers = -1
    executor.config.cache_type_k = "f16"
    executor.config.cache_type_v = "f16"
    return executor
```

- [ ] **Step 2: Verify test infrastructure works**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/conftest.py --co -q`
Expected: No errors (conftest loaded successfully)

- [ ] **Step 3: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add tests/conftest.py
git commit -m "test: add shared fixtures for adaptive quantization engine"
```

---

### Task 2: Quantization Intelligence — Data Models

**Files:**
- Create: `jarvis_prime/core/quantization_intelligence.py`
- Create: `tests/test_quantization_intelligence.py`

- [ ] **Step 1: Write failing tests for data models**

```python
"""Tests for quantization_intelligence.py — rate-distortion scoring engine."""
from __future__ import annotations

import pytest

from jarvis_prime.core.quantization_intelligence import (
    CalibrationData,
    CalibrationPoint,
    QuantizationProfile,
    QuantizationQualityScore,
    KNOWN_PROFILES,
    score_quantization,
    rank_quantizations,
    estimate_throughput,
)


class TestQuantizationProfile:
    """Test QuantizationProfile frozen dataclass."""

    def test_iq2_m_profile_exists(self):
        profile = KNOWN_PROFILES["IQ2_M"]
        assert profile.bits_per_weight == 2.70
        assert profile.uses_importance_matrix is True
        assert 0.0 < profile.quality_floor < profile.quality_ceiling <= 1.0

    def test_q4_k_m_profile_exists(self):
        profile = KNOWN_PROFILES["Q4_K_M"]
        assert profile.bits_per_weight == 4.83
        assert profile.uses_importance_matrix is False

    def test_profile_is_frozen(self):
        profile = KNOWN_PROFILES["IQ2_M"]
        with pytest.raises(AttributeError):
            profile.bits_per_weight = 5.0

    def test_all_profiles_have_valid_ranges(self):
        for name, p in KNOWN_PROFILES.items():
            assert p.bits_per_weight > 0, f"{name}: bpw must be positive"
            assert 0.0 < p.compression_ratio < 1.0, f"{name}: compression_ratio must be (0,1)"
            assert p.quality_floor <= p.quality_ceiling, f"{name}: floor > ceiling"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quantization_intelligence.py::TestQuantizationProfile -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis_prime.core.quantization_intelligence'`

- [ ] **Step 3: Implement data models**

```python
"""
Quantization Intelligence — Rate-Distortion Scoring Engine
==========================================================

Pure, deterministic, side-effect-free module that scores quantization
variants using information-theoretic metrics.

Mathematical foundation:
  R(D) = minimum bit-rate to achieve distortion ≤ D
  ppl(bpw) ≈ ppl_fp16 × (1 + α × (fp16_bpw / bpw)^β)

Where:
  α ≈ 0.015 (model-family coefficient)
  β ≈ 2.1   (distortion exponent)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass(frozen=True)
class QuantizationProfile:
    """Immutable descriptor for a quantization method."""
    name: str
    bits_per_weight: float
    compression_ratio: float       # relative to FP16 (0.0-1.0)
    uses_importance_matrix: bool   # True for IQ variants
    quality_floor: float           # Minimum quality estimate (0.0-1.0)
    quality_ceiling: float         # Maximum quality estimate (0.0-1.0)


@dataclass(frozen=True)
class CalibrationPoint:
    """Empirical measurement for a specific quant variant."""
    quant_name: str
    measured_tok_s: float
    measured_perplexity: Optional[float]
    measured_vram_bytes: int
    context_size: int
    timestamp: float


@dataclass(frozen=True)
class CalibrationData:
    """Empirical measurements that override theoretical estimates."""
    model_family: str
    measurements: Dict[str, CalibrationPoint]   # quant_name → point


@dataclass(frozen=True)
class QuantizationQualityScore:
    """Computed quality assessment for a specific model+quant combination."""
    profile: QuantizationProfile
    model_family: str
    estimated_perplexity_ratio: float   # ppl(quant) / ppl(fp16), ≥1.0
    quality_score: float                # 0.0-1.0
    vram_bytes: int                     # Model weight footprint
    estimated_tok_s: float              # Throughput estimate
    context_headroom_tokens: int        # Max context with f16 KV cache
    fitness_score: float                # Composite score
    scoring_basis: str                  # "empirical" | "interpolated" | "extrapolated"


# =============================================================================
# KNOWN PROFILES
# =============================================================================

KNOWN_PROFILES: Dict[str, QuantizationProfile] = {
    "IQ2_XXS": QuantizationProfile(
        name="IQ2_XXS", bits_per_weight=2.06, compression_ratio=0.129,
        uses_importance_matrix=True, quality_floor=0.55, quality_ceiling=0.70,
    ),
    "IQ2_M": QuantizationProfile(
        name="IQ2_M", bits_per_weight=2.70, compression_ratio=0.169,
        uses_importance_matrix=True, quality_floor=0.65, quality_ceiling=0.80,
    ),
    "Q2_K": QuantizationProfile(
        name="Q2_K", bits_per_weight=2.96, compression_ratio=0.185,
        uses_importance_matrix=False, quality_floor=0.60, quality_ceiling=0.75,
    ),
    "Q3_K_S": QuantizationProfile(
        name="Q3_K_S", bits_per_weight=3.50, compression_ratio=0.219,
        uses_importance_matrix=False, quality_floor=0.70, quality_ceiling=0.85,
    ),
    "Q3_K_M": QuantizationProfile(
        name="Q3_K_M", bits_per_weight=3.89, compression_ratio=0.243,
        uses_importance_matrix=False, quality_floor=0.75, quality_ceiling=0.88,
    ),
    "Q4_K_S": QuantizationProfile(
        name="Q4_K_S", bits_per_weight=4.58, compression_ratio=0.286,
        uses_importance_matrix=False, quality_floor=0.82, quality_ceiling=0.93,
    ),
    "Q4_K_M": QuantizationProfile(
        name="Q4_K_M", bits_per_weight=4.83, compression_ratio=0.302,
        uses_importance_matrix=False, quality_floor=0.85, quality_ceiling=0.95,
    ),
    "Q5_K_M": QuantizationProfile(
        name="Q5_K_M", bits_per_weight=5.69, compression_ratio=0.356,
        uses_importance_matrix=False, quality_floor=0.90, quality_ceiling=0.97,
    ),
    "Q6_K": QuantizationProfile(
        name="Q6_K", bits_per_weight=6.56, compression_ratio=0.410,
        uses_importance_matrix=False, quality_floor=0.93, quality_ceiling=0.98,
    ),
    "Q8_0": QuantizationProfile(
        name="Q8_0", bits_per_weight=8.50, compression_ratio=0.531,
        uses_importance_matrix=False, quality_floor=0.97, quality_ceiling=0.99,
    ),
}


# =============================================================================
# MODEL FAMILY COEFFICIENTS
# =============================================================================

_MODEL_FAMILY_COEFFICIENTS: Dict[str, Tuple[float, float]] = {
    # (alpha, beta) for ppl(bpw) ≈ ppl_fp16 × (1 + α × (16/bpw)^β)
    "qwen2.5-coder-32b": (0.015, 2.1),
    "qwen2.5-coder-14b": (0.018, 2.0),
    "qwen2.5-coder-7b": (0.022, 1.9),
    "deepseek-r1-qwen-7b": (0.020, 2.0),
    "llama-3.2-1b": (0.030, 1.8),
}

_DEFAULT_COEFFICIENTS: Tuple[float, float] = (0.020, 2.0)

# L4 GPU specs
_L4_MEMORY_BANDWIDTH_GBPS: float = 300.0
_L4_COMPUTE_TFLOPS: float = 30.3
_FP16_BPW: float = 16.0
_OVERHEAD_BYTES: int = 500_000_000  # 500MB CUDA/framework overhead
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quantization_intelligence.py::TestQuantizationProfile -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/quantization_intelligence.py tests/test_quantization_intelligence.py
git commit -m "feat: add quantization intelligence data models and known profiles"
```

---

### Task 3: Quantization Intelligence — Scoring Functions

**Files:**
- Modify: `jarvis_prime/core/quantization_intelligence.py`
- Modify: `tests/test_quantization_intelligence.py`

- [ ] **Step 1: Write failing tests for scoring**

Append to `tests/test_quantization_intelligence.py`:

```python
class TestEstimateThroughput:
    """Test roofline model throughput estimation."""

    def test_7b_q4_throughput(self):
        """7B Q4_K_M on L4 should estimate ~40-60 tok/s."""
        tok_s = estimate_throughput(
            model_params_billions=7.0,
            bits_per_weight=4.83,
            gpu_memory_bandwidth_gbps=300.0,
            gpu_compute_tflops=30.3,
        )
        assert 30.0 < tok_s < 80.0

    def test_32b_iq2_throughput(self):
        """32B IQ2_M on L4 should estimate ~8-20 tok/s."""
        tok_s = estimate_throughput(
            model_params_billions=32.0,
            bits_per_weight=2.70,
            gpu_memory_bandwidth_gbps=300.0,
            gpu_compute_tflops=30.3,
        )
        assert 5.0 < tok_s < 30.0

    def test_higher_bpw_means_lower_throughput_same_model(self):
        """Same model size, higher bpw → lower throughput (more memory to read)."""
        tok_s_q4 = estimate_throughput(32.0, 4.83, 300.0, 30.3)
        tok_s_iq2 = estimate_throughput(32.0, 2.70, 300.0, 30.3)
        assert tok_s_iq2 > tok_s_q4


class TestScoreQuantization:
    """Test composite quality scoring."""

    def test_q4_k_m_scores_higher_quality_than_iq2_m(self):
        """Higher bpw → higher quality score."""
        q4 = score_quantization(
            profile=KNOWN_PROFILES["Q4_K_M"],
            model_family="qwen2.5-coder-32b",
            model_size_bytes=19_000_000_000,
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        iq2 = score_quantization(
            profile=KNOWN_PROFILES["IQ2_M"],
            model_family="qwen2.5-coder-32b",
            model_size_bytes=11_000_000_000,
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        assert q4.quality_score > iq2.quality_score

    def test_model_that_doesnt_fit_gets_zero_fitness(self):
        """Model larger than VRAM should have fitness_score = 0."""
        score = score_quantization(
            profile=KNOWN_PROFILES["Q4_K_M"],
            model_family="qwen2.5-coder-32b",
            model_size_bytes=19_000_000_000,
            total_vram_bytes=15_000_000_000,  # Only 15GB VRAM
        )
        assert score.fitness_score == 0.0

    def test_scoring_basis_without_calibration(self):
        """Without calibration data, basis should be extrapolated."""
        score = score_quantization(
            profile=KNOWN_PROFILES["IQ2_M"],
            model_family="qwen2.5-coder-32b",
            model_size_bytes=11_000_000_000,
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        assert score.scoring_basis in ("interpolated", "extrapolated")

    def test_scoring_with_calibration(self):
        """With calibration data, basis should be empirical."""
        cal = CalibrationData(
            model_family="qwen2.5-coder-32b",
            measurements={
                "IQ2_M": CalibrationPoint(
                    quant_name="IQ2_M",
                    measured_tok_s=12.5,
                    measured_perplexity=None,
                    measured_vram_bytes=21_474 * 1024 * 1024,
                    context_size=8192,
                    timestamp=1710400000.0,
                ),
            },
        )
        score = score_quantization(
            profile=KNOWN_PROFILES["IQ2_M"],
            model_family="qwen2.5-coder-32b",
            model_size_bytes=11_000_000_000,
            total_vram_bytes=23_034 * 1024 * 1024,
            calibration_data=cal,
        )
        assert score.scoring_basis == "empirical"
        assert abs(score.estimated_tok_s - 12.5) < 0.01

    def test_perplexity_ratio_always_gte_one(self):
        for name, profile in KNOWN_PROFILES.items():
            score = score_quantization(
                profile=profile,
                model_family="qwen2.5-coder-7b",
                model_size_bytes=4_400_000_000,
                total_vram_bytes=23_034 * 1024 * 1024,
            )
            assert score.estimated_perplexity_ratio >= 1.0, f"{name}: ppl ratio < 1.0"


class TestRankQuantizations:
    """Test ranking of multiple quantization variants."""

    def test_rank_excludes_models_that_dont_fit(self):
        """Models exceeding VRAM should not appear in ranking."""
        available = [
            (KNOWN_PROFILES["Q4_K_M"], 19_000_000_000),
            (KNOWN_PROFILES["IQ2_M"], 11_000_000_000),
            (KNOWN_PROFILES["Q8_0"], 30_000_000_000),  # Too large
        ]
        ranked = rank_quantizations(
            available=available,
            model_family="qwen2.5-coder-32b",
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        names = [r.profile.name for r in ranked]
        assert "Q8_0" not in names

    def test_rank_returns_best_first(self):
        """First result should have highest fitness_score."""
        available = [
            (KNOWN_PROFILES["Q4_K_M"], 19_000_000_000),
            (KNOWN_PROFILES["IQ2_M"], 11_000_000_000),
        ]
        ranked = rank_quantizations(
            available=available,
            model_family="qwen2.5-coder-32b",
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        assert len(ranked) >= 1
        for i in range(len(ranked) - 1):
            assert ranked[i].fitness_score >= ranked[i + 1].fitness_score

    def test_empty_available_returns_empty(self):
        ranked = rank_quantizations(
            available=[],
            model_family="qwen2.5-coder-32b",
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        assert ranked == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quantization_intelligence.py::TestEstimateThroughput -v 2>&1 | head -10`
Expected: FAIL (functions not yet implemented)

- [ ] **Step 3: Implement scoring functions**

Append to `jarvis_prime/core/quantization_intelligence.py`:

```python
# =============================================================================
# SCORING FUNCTIONS
# =============================================================================

def _get_coefficients(model_family: str) -> Tuple[float, float]:
    """Get alpha, beta for a model family."""
    key = model_family.lower().replace(" ", "-")
    for family_key, coeffs in _MODEL_FAMILY_COEFFICIENTS.items():
        if family_key in key:
            return coeffs
    return _DEFAULT_COEFFICIENTS


def _estimate_perplexity_ratio(
    bits_per_weight: float,
    alpha: float,
    beta: float,
) -> float:
    """
    Estimate ppl(quant) / ppl(fp16) using power law.

    ppl(bpw) ≈ ppl_fp16 × (1 + α × (16/bpw)^β)
    ratio = ppl(bpw) / ppl_fp16 = 1 + α × (16/bpw)^β
    """
    ratio = 1.0 + alpha * (_FP16_BPW / bits_per_weight) ** beta
    return max(ratio, 1.0)


def _perplexity_ratio_to_quality(ratio: float) -> float:
    """
    Map perplexity ratio to a 0-1 quality score.

    Uses exponential decay: quality = exp(-k × (ratio - 1))
    where k controls sensitivity. At ratio=1.0 → quality=1.0.
    """
    k = 8.0  # Tuned so ratio=1.10 → quality≈0.45, ratio=1.02 → quality≈0.85
    return math.exp(-k * (ratio - 1.0))


def _estimate_context_headroom(
    model_size_bytes: int,
    total_vram_bytes: int,
    n_layers: int = 64,
    n_kv_heads: int = 8,
    head_dim: int = 128,
) -> int:
    """Estimate max context tokens with f16 KV cache."""
    available = total_vram_bytes - model_size_bytes - _OVERHEAD_BYTES
    if available <= 0:
        return 0
    # KV per token = 2 × n_layers × n_kv_heads × head_dim × 2 (f16)
    kv_per_token = 2 * n_layers * n_kv_heads * head_dim * 2
    if kv_per_token == 0:
        return 0
    return max(0, int(available / kv_per_token))


def estimate_throughput(
    model_params_billions: float,
    bits_per_weight: float,
    gpu_memory_bandwidth_gbps: float,
    gpu_compute_tflops: float,
) -> float:
    """
    Estimate tok/s using roofline model.

    LLM decode is memory-bandwidth-bound:
      tok/s ≈ memory_bandwidth / (model_params × bpw / 8)
    """
    model_bytes = model_params_billions * 1e9 * bits_per_weight / 8.0
    if model_bytes <= 0:
        return 0.0
    bandwidth_bytes_per_s = gpu_memory_bandwidth_gbps * 1e9
    tok_s = bandwidth_bytes_per_s / model_bytes
    return tok_s


def score_quantization(
    profile: QuantizationProfile,
    model_family: str,
    model_size_bytes: int,
    total_vram_bytes: int,
    target_context: int = 8192,
    task_complexity: str = "medium",
    calibration_data: Optional[CalibrationData] = None,
) -> QuantizationQualityScore:
    """
    Score a quantization variant for the given hardware and task.
    Pure function — no I/O, no state mutation.
    """
    alpha, beta = _get_coefficients(model_family)

    # Check if model fits
    fits = (model_size_bytes + _OVERHEAD_BYTES) < total_vram_bytes

    # Perplexity ratio
    ppl_ratio = _estimate_perplexity_ratio(profile.bits_per_weight, alpha, beta)
    quality = _perplexity_ratio_to_quality(ppl_ratio)

    # Context headroom
    context_headroom = _estimate_context_headroom(
        model_size_bytes, total_vram_bytes,
    ) if fits else 0

    # Throughput estimate
    scoring_basis = "extrapolated"
    tok_s = estimate_throughput(
        model_params_billions=model_size_bytes / (profile.bits_per_weight / 8.0) / 1e9,
        bits_per_weight=profile.bits_per_weight,
        gpu_memory_bandwidth_gbps=_L4_MEMORY_BANDWIDTH_GBPS,
        gpu_compute_tflops=_L4_COMPUTE_TFLOPS,
    )

    # Override with calibration if available
    if calibration_data and profile.name in calibration_data.measurements:
        cal = calibration_data.measurements[profile.name]
        tok_s = cal.measured_tok_s
        scoring_basis = "empirical"

    # Task complexity weights
    complexity_weights = {
        "trivial": {"quality": 0.2, "throughput": 0.6, "context": 0.2},
        "light": {"quality": 0.3, "throughput": 0.5, "context": 0.2},
        "medium": {"quality": 0.4, "throughput": 0.3, "context": 0.3},
        "heavy": {"quality": 0.6, "throughput": 0.1, "context": 0.3},
        "complex": {"quality": 0.7, "throughput": 0.1, "context": 0.2},
    }
    weights = complexity_weights.get(task_complexity, complexity_weights["medium"])

    # Fitness score
    if not fits:
        fitness = 0.0
    else:
        # Normalize throughput (0-1 scale, assuming max ~60 tok/s on L4)
        norm_tok_s = min(tok_s / 60.0, 1.0)
        # Normalize context (0-1 scale relative to target)
        norm_ctx = min(context_headroom / max(target_context, 1), 1.0)
        fitness = (
            weights["quality"] * quality
            + weights["throughput"] * norm_tok_s
            + weights["context"] * norm_ctx
        )

    return QuantizationQualityScore(
        profile=profile,
        model_family=model_family,
        estimated_perplexity_ratio=ppl_ratio,
        quality_score=quality,
        vram_bytes=model_size_bytes,
        estimated_tok_s=tok_s,
        context_headroom_tokens=context_headroom,
        fitness_score=fitness,
        scoring_basis=scoring_basis,
    )


def rank_quantizations(
    available: List[Tuple[QuantizationProfile, int]],
    model_family: str,
    total_vram_bytes: int,
    target_context: int = 8192,
    task_complexity: str = "medium",
    calibration_data: Optional[CalibrationData] = None,
) -> List[QuantizationQualityScore]:
    """
    Rank all available quantizations by fitness_score.
    Returns sorted list, best first. Excludes variants that won't fit.
    """
    scores = []
    for profile, file_size in available:
        score = score_quantization(
            profile=profile,
            model_family=model_family,
            model_size_bytes=file_size,
            total_vram_bytes=total_vram_bytes,
            target_context=target_context,
            task_complexity=task_complexity,
            calibration_data=calibration_data,
        )
        if score.fitness_score > 0.0:
            scores.append(score)

    scores.sort(key=lambda s: s.fitness_score, reverse=True)
    return scores
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quantization_intelligence.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/quantization_intelligence.py tests/test_quantization_intelligence.py
git commit -m "feat: implement quantization intelligence scoring engine with rate-distortion model"
```

---

## Chunk 2: KV Cache Optimizer

### Task 4: KV Cache Optimizer — Full Implementation

**Files:**
- Create: `jarvis_prime/core/kv_cache_optimizer.py`
- Create: `tests/test_kv_cache_optimizer.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for kv_cache_optimizer.py — context window maximizer."""
from __future__ import annotations

import pytest

from jarvis_prime.core.kv_cache_optimizer import (
    KVCacheType,
    KVCacheProfile,
    ModelArchitectureParams,
    compute_kv_bytes_per_token,
    compute_feasible_profiles,
)


class TestModelArchitectureParams:
    """Test architecture parameter handling."""

    def test_qwen_32b_params(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        assert params.n_layers == 64
        assert params.n_kv_heads == 8

    def test_frozen(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        with pytest.raises(AttributeError):
            params.n_layers = 32


class TestKVBytesPerToken:
    """Test per-token KV cache size computation."""

    def test_f16_qwen_32b(self):
        """Qwen2.5-32B f16 KV: 2 × 64 × 8 × 128 × 2 = 262,144 bytes."""
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        result = compute_kv_bytes_per_token(params, KVCacheType.F16)
        assert result == 262_144

    def test_q8_halves_size(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        f16 = compute_kv_bytes_per_token(params, KVCacheType.F16)
        q8 = compute_kv_bytes_per_token(params, KVCacheType.Q8_0)
        assert q8 == f16 // 2

    def test_q4_quarters_size(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        f16 = compute_kv_bytes_per_token(params, KVCacheType.F16)
        q4 = compute_kv_bytes_per_token(params, KVCacheType.Q4_0)
        assert q4 == f16 // 4


class TestComputeFeasibleProfiles:
    """Test feasible KV cache profile generation."""

    def test_returns_profiles_sorted_by_quality(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        profiles = compute_feasible_profiles(
            model_params=params,
            model_weight_bytes=11_000_000_000,  # 11GB IQ2_M
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        assert len(profiles) >= 1
        # First should be best quality (lowest quality_impact)
        for i in range(len(profiles) - 1):
            assert profiles[i].quality_impact <= profiles[i + 1].quality_impact

    def test_excludes_profiles_below_min_context(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        profiles = compute_feasible_profiles(
            model_params=params,
            model_weight_bytes=22_000_000_000,  # Barely fits
            total_vram_bytes=23_034 * 1024 * 1024,
            min_context=2048,
        )
        for p in profiles:
            assert p.max_context_tokens >= 2048

    def test_iq2_m_on_l4_supports_8k_context(self):
        """IQ2_M (11GB) on L4 (23GB) should support 8192 context."""
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        profiles = compute_feasible_profiles(
            model_params=params,
            model_weight_bytes=11_000_000_000,
            total_vram_bytes=23_034 * 1024 * 1024,
            target_context=8192,
        )
        assert any(p.max_context_tokens >= 8192 for p in profiles)

    def test_no_vram_returns_empty(self):
        params = ModelArchitectureParams(
            n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
        )
        profiles = compute_feasible_profiles(
            model_params=params,
            model_weight_bytes=25_000_000_000,  # Exceeds VRAM
            total_vram_bytes=23_034 * 1024 * 1024,
        )
        assert profiles == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_kv_cache_optimizer.py -v 2>&1 | head -10`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement kv_cache_optimizer.py**

```python
"""
KV Cache Optimizer — Context Window Maximizer
==============================================

Pure, deterministic, side-effect-free module that computes feasible
KV cache configurations given model architecture and VRAM constraints.

KV cache memory per token:
  kv_per_token = 2 × n_layers × n_kv_heads × head_dim × bytes_per_element
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List

logger = logging.getLogger(__name__)


# =============================================================================
# DATA MODELS
# =============================================================================

class KVCacheType(Enum):
    F16 = "f16"     # Full precision, best quality
    Q8_0 = "q8_0"   # 50% savings, ~0.1% quality loss
    Q4_0 = "q4_0"   # 75% savings, ~0.5% quality loss


# Bytes per element for each cache type
_BYTES_PER_ELEMENT = {
    KVCacheType.F16: 2,
    KVCacheType.Q8_0: 1,
    KVCacheType.Q4_0: 0.5,
}

# Quality impact (0.0 = no impact, higher = worse)
_QUALITY_IMPACT = {
    KVCacheType.F16: 0.0,
    KVCacheType.Q8_0: 0.001,
    KVCacheType.Q4_0: 0.005,
}


@dataclass(frozen=True)
class ModelArchitectureParams:
    """Model architecture parameters for KV cache computation."""
    n_layers: int
    n_heads: int
    n_kv_heads: int     # For GQA models (< n_heads)
    head_dim: int
    vocab_size: int


@dataclass(frozen=True)
class KVCacheProfile:
    """Feasible KV cache configuration for given constraints."""
    cache_type_k: KVCacheType
    cache_type_v: KVCacheType
    max_context_tokens: int
    vram_bytes: int              # Total KV cache VRAM at max_context
    quality_impact: float        # 0.0 = no impact, 1.0 = severe
    recommendation: str          # Human-readable


# =============================================================================
# KNOWN ARCHITECTURES
# =============================================================================

KNOWN_ARCHITECTURES = {
    "qwen2.5-coder-32b": ModelArchitectureParams(
        n_layers=64, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
    ),
    "qwen2.5-coder-14b": ModelArchitectureParams(
        n_layers=48, n_heads=40, n_kv_heads=8, head_dim=128, vocab_size=152064,
    ),
    "qwen2.5-coder-7b": ModelArchitectureParams(
        n_layers=28, n_heads=28, n_kv_heads=4, head_dim=128, vocab_size=152064,
    ),
    "deepseek-r1-qwen-7b": ModelArchitectureParams(
        n_layers=28, n_heads=28, n_kv_heads=4, head_dim=128, vocab_size=152064,
    ),
    "llama-3.2-1b": ModelArchitectureParams(
        n_layers=16, n_heads=32, n_kv_heads=8, head_dim=64, vocab_size=128256,
    ),
}


# =============================================================================
# COMPUTATION FUNCTIONS
# =============================================================================

def compute_kv_bytes_per_token(
    params: ModelArchitectureParams,
    cache_type: KVCacheType,
) -> int:
    """
    Compute KV cache bytes per token.

    Formula: 2 × n_layers × n_kv_heads × head_dim × bytes_per_element
    The factor of 2 is for K and V caches.
    """
    bpe = _BYTES_PER_ELEMENT[cache_type]
    return int(2 * params.n_layers * params.n_kv_heads * params.head_dim * bpe)


def compute_feasible_profiles(
    model_params: ModelArchitectureParams,
    model_weight_bytes: int,
    total_vram_bytes: int,
    overhead_bytes: int = 500_000_000,
    target_context: int = 8192,
    min_context: int = 2048,
) -> List[KVCacheProfile]:
    """
    Compute all feasible KV cache profiles.

    Returns profiles sorted by quality (best first), filtered to those
    that achieve at least min_context tokens.
    """
    available_for_kv = total_vram_bytes - model_weight_bytes - overhead_bytes
    if available_for_kv <= 0:
        return []

    # Generate profiles for all K×V type combinations
    profiles: List[KVCacheProfile] = []

    # Only consider symmetric K/V types (k=v) for simplicity
    for cache_type in KVCacheType:
        bpt = compute_kv_bytes_per_token(model_params, cache_type)
        if bpt <= 0:
            continue

        max_tokens = int(available_for_kv / bpt)
        if max_tokens < min_context:
            continue

        # Cap at a reasonable maximum
        max_tokens = min(max_tokens, 131072)

        impact = _QUALITY_IMPACT[cache_type]
        vram_at_target = min(max_tokens, target_context) * bpt

        # Build recommendation string
        if max_tokens >= target_context:
            rec = f"{cache_type.value}: full {target_context}-token context ({max_tokens} max)"
        else:
            rec = f"{cache_type.value}: reduced to {max_tokens} tokens (target: {target_context})"

        profiles.append(KVCacheProfile(
            cache_type_k=cache_type,
            cache_type_v=cache_type,
            max_context_tokens=max_tokens,
            vram_bytes=vram_at_target,
            quality_impact=impact,
            recommendation=rec,
        ))

    # Sort by quality impact (lower = better)
    profiles.sort(key=lambda p: p.quality_impact)
    return profiles
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_kv_cache_optimizer.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/kv_cache_optimizer.py tests/test_kv_cache_optimizer.py
git commit -m "feat: implement KV cache optimizer with context window maximization"
```

---

## Chunk 3: VRAM Pressure Monitor

### Task 5: VRAM Pressure Monitor

**Files:**
- Create: `jarvis_prime/core/vram_pressure_monitor.py`
- Create: `tests/test_vram_pressure_monitor.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for vram_pressure_monitor.py — GPU memory watchdog."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis_prime.core.vram_pressure_monitor import (
    VRAMPressureZone,
    VRAMPressureEvent,
    VRAMMonitorConfig,
    VRAMPressureMonitor,
    estimate_effective_free,
)


class TestEstimateEffectiveFree:

    def test_post_unload_high_usability(self):
        free = estimate_effective_free(10_000_000_000, model_loaded=False)
        assert free == int(10_000_000_000 * 0.95)

    def test_with_model_loaded_conservative(self):
        free = estimate_effective_free(5_000_000_000, model_loaded=True)
        assert free == int(5_000_000_000 * 0.80)


class TestVRAMPressureZone:

    def test_zone_from_utilization(self):
        config = VRAMMonitorConfig()
        assert VRAMPressureMonitor._zone_for_utilization(0.50, config) == VRAMPressureZone.GREEN
        assert VRAMPressureMonitor._zone_for_utilization(0.75, config) == VRAMPressureZone.YELLOW
        assert VRAMPressureMonitor._zone_for_utilization(0.88, config) == VRAMPressureZone.RED
        assert VRAMPressureMonitor._zone_for_utilization(0.95, config) == VRAMPressureZone.CRITICAL


class TestVRAMPressureMonitor:

    @pytest.mark.asyncio
    async def test_mock_backend_reports_zones(self):
        """Mock backend should allow manual VRAM setting."""
        config = VRAMMonitorConfig(backend="mock")
        monitor = VRAMPressureMonitor(config=config, node_id="test-node")
        monitor.set_mock_vram(total=23_000_000_000, used=10_000_000_000)

        snapshot = await monitor.sample()
        assert snapshot.zone == VRAMPressureZone.GREEN

    @pytest.mark.asyncio
    async def test_event_emitted_on_zone_change(self):
        """Zone transition should emit a VRAMPressureEvent."""
        config = VRAMMonitorConfig(backend="mock", sustained_threshold_s=0.0)
        monitor = VRAMPressureMonitor(config=config, node_id="test-node")
        events: list[VRAMPressureEvent] = []
        monitor.on_pressure_change(events.append)

        # Start in GREEN
        monitor.set_mock_vram(total=23_000_000_000, used=10_000_000_000)
        await monitor.sample()

        # Jump to RED
        monitor.set_mock_vram(total=23_000_000_000, used=20_000_000_000)
        await monitor.sample()

        assert len(events) >= 1
        assert events[-1].zone == VRAMPressureZone.RED
        assert events[-1].node_id == "test-node"

    @pytest.mark.asyncio
    async def test_no_event_on_same_zone(self):
        """No event when zone stays the same."""
        config = VRAMMonitorConfig(backend="mock", sustained_threshold_s=0.0)
        monitor = VRAMPressureMonitor(config=config, node_id="test-node")
        events: list[VRAMPressureEvent] = []
        monitor.on_pressure_change(events.append)

        monitor.set_mock_vram(total=23_000_000_000, used=10_000_000_000)
        await monitor.sample()
        await monitor.sample()

        # Only initial transition from UNKNOWN → GREEN
        assert len(events) <= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_vram_pressure_monitor.py -v 2>&1 | head -10`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement vram_pressure_monitor.py**

```python
"""
VRAM Pressure Monitor — GPU Memory Watchdog
============================================

Read-only module that monitors GPU VRAM usage and emits pressure events.
Never triggers swaps directly — events are consumed by ModelTransitionManager.

Backends: pynvml (preferred), nvidia-smi (fallback), mock (testing).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


# =============================================================================
# DATA MODELS
# =============================================================================

class VRAMPressureZone(Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    CRITICAL = "critical"


@dataclass(frozen=True)
class VRAMPressureEvent:
    """Emitted when pressure zone changes. Advisory only."""
    zone: VRAMPressureZone
    previous_zone: VRAMPressureZone
    total_bytes: int
    used_bytes: int
    free_bytes: int
    fragmentation_estimate: float
    model_resident_bytes: int
    kv_cache_bytes: int
    timestamp: float
    sustained_seconds: float
    node_id: str


@dataclass
class VRAMMonitorConfig:
    poll_interval_s: float = field(default_factory=lambda: _env_float("JARVIS_VRAM_POLL_INTERVAL_S", 5.0))
    critical_poll_interval_s: float = 1.0
    zone_thresholds: dict = field(default_factory=lambda: {
        "yellow": _env_float("JARVIS_VRAM_YELLOW_THRESHOLD", 0.70),
        "red": _env_float("JARVIS_VRAM_RED_THRESHOLD", 0.85),
        "critical": _env_float("JARVIS_VRAM_CRITICAL_THRESHOLD", 0.92),
    })
    sustained_threshold_s: float = 10.0
    backend: str = "pynvml"


@dataclass(frozen=True)
class VRAMSnapshot:
    """Point-in-time VRAM reading."""
    zone: VRAMPressureZone
    total_bytes: int
    used_bytes: int
    free_bytes: int
    utilization: float
    timestamp: float


# =============================================================================
# HELPERS
# =============================================================================

def estimate_effective_free(free_bytes: int, model_loaded: bool) -> int:
    """Conservative estimate of allocatable VRAM."""
    if not model_loaded:
        return int(free_bytes * 0.95)
    return int(free_bytes * 0.80)


# =============================================================================
# MONITOR
# =============================================================================

class VRAMPressureMonitor:
    """GPU memory watchdog. Emits VRAMPressureEvents on zone transitions."""

    def __init__(
        self,
        config: Optional[VRAMMonitorConfig] = None,
        node_id: str = "gcp-jarvis-prime-stable",
    ):
        self._config = config or VRAMMonitorConfig()
        self._node_id = node_id
        self._current_zone = VRAMPressureZone.GREEN
        self._zone_entered_at: float = time.monotonic()
        self._callbacks: List[Callable[[VRAMPressureEvent], None]] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Mock backend state
        self._mock_total: int = 0
        self._mock_used: int = 0
        self._initialized = False

    def on_pressure_change(self, callback: Callable[[VRAMPressureEvent], None]) -> None:
        self._callbacks.append(callback)

    def set_mock_vram(self, total: int, used: int) -> None:
        self._mock_total = total
        self._mock_used = used

    @staticmethod
    def _zone_for_utilization(util: float, config: VRAMMonitorConfig) -> VRAMPressureZone:
        if util >= config.zone_thresholds["critical"]:
            return VRAMPressureZone.CRITICAL
        if util >= config.zone_thresholds["red"]:
            return VRAMPressureZone.RED
        if util >= config.zone_thresholds["yellow"]:
            return VRAMPressureZone.YELLOW
        return VRAMPressureZone.GREEN

    async def _read_vram(self) -> tuple[int, int]:
        """Read (total, used) VRAM bytes from configured backend."""
        backend = self._config.backend

        if backend == "mock":
            return self._mock_total, self._mock_used

        if backend == "pynvml":
            try:
                import pynvml
                pynvml.nvmlInit()
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                return info.total, info.used
            except Exception:
                logger.warning("[VRAMMonitor] pynvml failed, falling back to nvidia-smi")
                backend = "nvidia_smi"

        if backend == "nvidia_smi":
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.total,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    parts = result.stdout.strip().split(",")
                    total_mib = float(parts[0].strip())
                    used_mib = float(parts[1].strip())
                    return int(total_mib * 1024 * 1024), int(used_mib * 1024 * 1024)
            except Exception:
                pass

        # Safe default
        logger.warning("[VRAMMonitor] All backends failed, assuming YELLOW")
        return 23_034 * 1024 * 1024, int(23_034 * 1024 * 1024 * 0.75)

    async def sample(self) -> VRAMSnapshot:
        """Take a single VRAM sample and emit events if zone changed."""
        total, used = await self._read_vram()
        free = total - used
        util = used / total if total > 0 else 0.0
        now = time.monotonic()

        new_zone = self._zone_for_utilization(util, self._config)
        snapshot = VRAMSnapshot(
            zone=new_zone, total_bytes=total, used_bytes=used,
            free_bytes=free, utilization=util, timestamp=time.time(),
        )

        if new_zone != self._current_zone:
            sustained = now - self._zone_entered_at
            if not self._initialized or sustained >= self._config.sustained_threshold_s:
                event = VRAMPressureEvent(
                    zone=new_zone,
                    previous_zone=self._current_zone,
                    total_bytes=total,
                    used_bytes=used,
                    free_bytes=free,
                    fragmentation_estimate=0.05 if used > 0 else 0.0,
                    model_resident_bytes=0,
                    kv_cache_bytes=0,
                    timestamp=time.time(),
                    sustained_seconds=sustained,
                    node_id=self._node_id,
                )
                for cb in self._callbacks:
                    try:
                        cb(event)
                    except Exception:
                        logger.exception("[VRAMMonitor] Callback error")
                self._current_zone = new_zone
                self._zone_entered_at = now
                self._initialized = True

        return snapshot

    async def start(self) -> None:
        """Start background monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"[VRAMMonitor] Started on {self._node_id}")

    async def stop(self) -> None:
        """Stop background monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.sample()
            except Exception:
                logger.exception("[VRAMMonitor] Poll error")
            interval = (
                self._config.critical_poll_interval_s
                if self._current_zone in (VRAMPressureZone.RED, VRAMPressureZone.CRITICAL)
                else self._config.poll_interval_s
            )
            await asyncio.sleep(interval)

    @property
    def current_zone(self) -> VRAMPressureZone:
        return self._current_zone
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_vram_pressure_monitor.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/vram_pressure_monitor.py tests/test_vram_pressure_monitor.py
git commit -m "feat: implement VRAM pressure monitor with zone-based events"
```

---

## Chunk 4: Adaptive Model Selector

### Task 6: Adaptive Model Selector — Inventory + Proposals

**Files:**
- Create: `jarvis_prime/core/adaptive_model_selector.py`
- Create: `tests/test_adaptive_model_selector.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for adaptive_model_selector.py — inventory + proposals."""
from __future__ import annotations

import pytest
from pathlib import Path

from jarvis_prime.core.adaptive_model_selector import (
    ModelVariant,
    ModelFamily,
    ModelSelectionProposal,
    parse_gguf_filename,
    scan_inventory,
    propose_optimal,
)
from jarvis_prime.core.quantization_intelligence import KNOWN_PROFILES


class TestParseGgufFilename:

    def test_standard_format(self):
        base, quant = parse_gguf_filename("Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf")
        assert base == "qwen2.5-coder-32b-instruct"
        assert quant == "Q4_K_M"

    def test_iq_format(self):
        base, quant = parse_gguf_filename("Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf")
        assert base == "qwen2.5-coder-32b-instruct"
        assert quant == "IQ2_M"

    def test_llama_format(self):
        base, quant = parse_gguf_filename("Llama-3.2-1B-Instruct-Q4_K_M.gguf")
        assert base == "llama-3.2-1b-instruct"
        assert quant == "Q4_K_M"

    def test_unknown_quant_returns_none(self):
        base, quant = parse_gguf_filename("some-random-model.gguf")
        assert quant is None


class TestScanInventory:

    @pytest.mark.asyncio
    async def test_groups_by_family(self, fake_gguf_files, tmp_models_dir):
        families = await scan_inventory(tmp_models_dir)
        family_names = {f.base_model for f in families}
        assert "qwen2.5-coder-32b-instruct" in family_names

    @pytest.mark.asyncio
    async def test_32b_has_two_variants(self, fake_gguf_files, tmp_models_dir):
        families = await scan_inventory(tmp_models_dir)
        family_32b = next(f for f in families if "32b" in f.base_model)
        assert len(family_32b.variants) == 2  # IQ2_M + Q4_K_M

    @pytest.mark.asyncio
    async def test_empty_dir(self, tmp_models_dir):
        families = await scan_inventory(tmp_models_dir)
        assert families == []


class TestProposeOptimal:

    @pytest.mark.asyncio
    async def test_proposes_model_that_fits(self, fake_gguf_files, tmp_models_dir):
        families = await scan_inventory(tmp_models_dir)
        proposal = await propose_optimal(
            families=families,
            vram_budget_bytes=23_034 * 1024 * 1024,
            target_context=8192,
            task_complexity="medium",
            current_model=None,
        )
        assert proposal is not None
        assert proposal.selected_variant.size_bytes < 23_034 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_proposal_has_reason(self, fake_gguf_files, tmp_models_dir):
        families = await scan_inventory(tmp_models_dir)
        proposal = await propose_optimal(
            families=families,
            vram_budget_bytes=23_034 * 1024 * 1024,
            target_context=8192,
            task_complexity="medium",
            current_model=None,
        )
        assert proposal is not None
        assert len(proposal.reason) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_adaptive_model_selector.py -v 2>&1 | head -10`
Expected: FAIL

- [ ] **Step 3: Implement adaptive_model_selector.py**

```python
"""
Adaptive Model Selector — Proposal Engine
==========================================

Scans model directory, groups by family, and proposes optimal
model selections. Read-only I/O. Proposes plans — never executes them.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from jarvis_prime.core.quantization_intelligence import (
    KNOWN_PROFILES,
    CalibrationData,
    QuantizationProfile,
    QuantizationQualityScore,
    rank_quantizations,
    score_quantization,
)
from jarvis_prime.core.kv_cache_optimizer import (
    KVCacheProfile,
    KVCacheType,
    KNOWN_ARCHITECTURES,
    compute_feasible_profiles,
)

logger = logging.getLogger(__name__)


# =============================================================================
# FILENAME PARSING
# =============================================================================

# Matches quant suffix before .gguf: -Q4_K_M, -IQ2_M, -Q8_0, etc.
_QUANT_PATTERN = re.compile(
    r"-("
    r"IQ[12345]_(?:XXS|XS|S|M|L|XL)"
    r"|Q[2345678]_(?:K_S|K_M|K_L|K|0)"
    r"|Q8_0"
    r"|F16|F32|BF16"
    r")\.gguf$",
    re.IGNORECASE,
)

# Extract parameter count from name
_PARAM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)[Bb]", re.IGNORECASE)


def parse_gguf_filename(filename: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a GGUF filename into (base_model, quant_name).

    Example: "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf"
             → ("qwen2.5-coder-32b-instruct", "Q4_K_M")
    """
    match = _QUANT_PATTERN.search(filename)
    if not match:
        return (filename.replace(".gguf", "").lower(), None)

    quant_name = match.group(1).upper()
    # Normalize: IQ2_M stays IQ2_M, Q4_K_M stays Q4_K_M
    base = filename[:match.start()].lower()
    return (base, quant_name)


def _extract_param_count(name: str) -> float:
    """Extract parameter count in billions from model name."""
    match = _PARAM_PATTERN.search(name)
    if match:
        return float(match.group(1))
    return 0.0


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass(frozen=True)
class ModelVariant:
    """A single GGUF file with parsed metadata."""
    path: Path
    size_bytes: int
    base_model: str
    quant_name: str
    quant_profile: QuantizationProfile
    sha256: Optional[str] = None
    provenance: str = "local"


@dataclass(frozen=True)
class ModelFamily:
    """All quantization variants of one base model."""
    base_model: str
    variants: Tuple[ModelVariant, ...]
    parameter_count: float


@dataclass(frozen=True)
class ModelSelectionProposal:
    """A proposed model change — advisory only, not executed."""
    proposal_id: str
    selected_variant: ModelVariant
    quality_score: QuantizationQualityScore
    kv_cache_profile: Optional[KVCacheProfile]
    reason: str
    trigger: str
    inventory_digest: str
    timestamp: float


# =============================================================================
# INVENTORY SCANNING
# =============================================================================

def _compute_inventory_digest(model_dir: Path) -> str:
    """SHA256 of sorted (filename, size) pairs."""
    entries = []
    for p in sorted(model_dir.glob("*.gguf")):
        if p.is_file():
            entries.append(f"{p.name}:{p.stat().st_size}")
    return hashlib.sha256("|".join(entries).encode()).hexdigest()[:16]


async def scan_inventory(model_dir: Path) -> List[ModelFamily]:
    """Scan model directory, group by family. Read-only."""
    families_map: Dict[str, List[ModelVariant]] = {}

    for path in sorted(model_dir.glob("*.gguf")):
        if not path.is_file() or path.name.startswith("."):
            continue

        base, quant_name = parse_gguf_filename(path.name)
        if not quant_name or quant_name.upper() not in KNOWN_PROFILES:
            logger.debug(f"[ModelSelector] Skipping {path.name}: unknown quant {quant_name}")
            continue

        profile = KNOWN_PROFILES[quant_name.upper()]
        variant = ModelVariant(
            path=path,
            size_bytes=path.stat().st_size,
            base_model=base,
            quant_name=quant_name.upper(),
            quant_profile=profile,
        )

        families_map.setdefault(base, []).append(variant)

    families = []
    for base, variants in families_map.items():
        # Sort by quality descending (higher bpw = better quality)
        variants.sort(key=lambda v: v.quant_profile.bits_per_weight, reverse=True)
        param_count = _extract_param_count(base)
        families.append(ModelFamily(
            base_model=base,
            variants=tuple(variants),
            parameter_count=param_count,
        ))

    return families


# =============================================================================
# PROPOSAL GENERATION
# =============================================================================

async def propose_optimal(
    families: List[ModelFamily],
    vram_budget_bytes: int,
    target_context: int = 8192,
    task_complexity: str = "medium",
    current_model: Optional[ModelVariant] = None,
    calibration: Optional[CalibrationData] = None,
    trigger: str = "startup",
    model_dir: Optional[Path] = None,
) -> Optional[ModelSelectionProposal]:
    """Propose the best model for current conditions. Does NOT execute."""
    # Collect all variants across families
    all_available: List[Tuple[QuantizationProfile, int, ModelVariant]] = []
    for family in families:
        for variant in family.variants:
            all_available.append((variant.quant_profile, variant.size_bytes, variant))

    if not all_available:
        return None

    # Score and rank
    ranked_pairs: List[Tuple[QuantizationQualityScore, ModelVariant]] = []
    for profile, size, variant in all_available:
        score = score_quantization(
            profile=profile,
            model_family=variant.base_model,
            model_size_bytes=size,
            total_vram_bytes=vram_budget_bytes,
            target_context=target_context,
            task_complexity=task_complexity,
            calibration_data=calibration,
        )
        if score.fitness_score > 0.0:
            ranked_pairs.append((score, variant))

    if not ranked_pairs:
        return None

    ranked_pairs.sort(key=lambda p: p[0].fitness_score, reverse=True)
    best_score, best_variant = ranked_pairs[0]

    # Compute KV cache profile
    kv_profile = None
    arch_key = best_variant.base_model.replace("-instruct", "")
    for key, params in KNOWN_ARCHITECTURES.items():
        if key in arch_key:
            profiles = compute_feasible_profiles(
                model_params=params,
                model_weight_bytes=best_variant.size_bytes,
                total_vram_bytes=vram_budget_bytes,
                target_context=target_context,
            )
            if profiles:
                kv_profile = profiles[0]  # Best quality
            break

    # Build reason
    reason = (
        f"Selected {best_variant.path.name} "
        f"(fitness={best_score.fitness_score:.3f}, "
        f"quality={best_score.quality_score:.3f}, "
        f"tok/s≈{best_score.estimated_tok_s:.1f}, "
        f"ctx≈{best_score.context_headroom_tokens})"
    )

    digest = _compute_inventory_digest(best_variant.path.parent) if model_dir else "unknown"

    return ModelSelectionProposal(
        proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
        selected_variant=best_variant,
        quality_score=best_score,
        kv_cache_profile=kv_profile,
        reason=reason,
        trigger=trigger,
        inventory_digest=digest,
        timestamp=time.time(),
    )
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_adaptive_model_selector.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/adaptive_model_selector.py tests/test_adaptive_model_selector.py
git commit -m "feat: implement adaptive model selector with inventory scanning and proposal engine"
```

---

## Chunk 5: Model Transition Manager (VRAMBudgetAuthority + FSM)

### Task 7: VRAMBudgetAuthority

**Files:**
- Create: `jarvis_prime/core/model_transition_manager.py`
- Create: `tests/test_model_transition_manager.py`

- [ ] **Step 1: Write failing tests for VRAMBudgetAuthority**

```python
"""Tests for model_transition_manager.py — FSM executor + VRAMBudgetAuthority."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis_prime.core.model_transition_manager import (
    LeaseState,
    VRAMGrant,
    VRAMPriority,
    VRAMBudgetAuthority,
    TransitionState,
    TransitionEpoch,
    TransitionPolicy,
    ModelTransitionManager,
)


class TestVRAMBudgetAuthority:

    @pytest.mark.asyncio
    async def test_grant_within_budget(self):
        auth = VRAMBudgetAuthority(total_vram_bytes=23_000_000_000)
        grant = await auth.request("model-iq2m", 11_000_000_000, VRAMPriority.NORMAL)
        assert grant is not None
        assert grant.state == LeaseState.GRANTED

    @pytest.mark.asyncio
    async def test_deny_exceeds_budget(self):
        auth = VRAMBudgetAuthority(total_vram_bytes=23_000_000_000)
        grant = await auth.request("model-q4km", 25_000_000_000, VRAMPriority.NORMAL)
        assert grant is None

    @pytest.mark.asyncio
    async def test_commit_transitions_to_active(self):
        auth = VRAMBudgetAuthority(total_vram_bytes=23_000_000_000)
        grant = await auth.request("model-test", 10_000_000_000, VRAMPriority.NORMAL)
        assert grant is not None
        await grant.commit(10_500_000_000)
        assert grant.state == LeaseState.ACTIVE

    @pytest.mark.asyncio
    async def test_release_frees_budget(self):
        auth = VRAMBudgetAuthority(total_vram_bytes=23_000_000_000)
        g1 = await auth.request("model-a", 15_000_000_000, VRAMPriority.NORMAL)
        assert g1 is not None
        # Second grant would exceed budget
        g2 = await auth.request("model-b", 15_000_000_000, VRAMPriority.NORMAL)
        assert g2 is None
        # Release first, try again
        await g1.release()
        g3 = await auth.request("model-b", 15_000_000_000, VRAMPriority.NORMAL)
        assert g3 is not None

    @pytest.mark.asyncio
    async def test_rollback_frees_budget(self):
        auth = VRAMBudgetAuthority(total_vram_bytes=23_000_000_000)
        grant = await auth.request("model-test", 10_000_000_000, VRAMPriority.NORMAL)
        assert grant is not None
        await grant.rollback("test failure")
        assert grant.state == LeaseState.ROLLED_BACK
        assert auth.allocated_bytes == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_model_transition_manager.py::TestVRAMBudgetAuthority -v 2>&1 | head -10`
Expected: FAIL

- [ ] **Step 3: Implement VRAMBudgetAuthority + FSM skeleton**

```python
"""
Model Transition Manager — The Single Executor
================================================

THE executor for all model changes. Serialized FSM with epoch-based
consistency, drain protocol, and VRAMBudgetAuthority integration.

States: IDLE → PREPARE → DRAIN → CUTOVER → VERIFY → COMMIT/ROLLBACK
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


# =============================================================================
# VRAM BUDGET AUTHORITY
# =============================================================================

class LeaseState(Enum):
    GRANTED = "granted"
    ACTIVE = "active"
    RELEASED = "released"
    ROLLED_BACK = "rolled_back"


class VRAMPriority(Enum):
    CRITICAL = 0
    NORMAL = 1
    BACKGROUND = 2


class VRAMGrant:
    """A VRAM lease with lifecycle management."""

    def __init__(
        self,
        grant_id: str,
        component: str,
        granted_bytes: int,
        ttl_seconds: float,
        authority: VRAMBudgetAuthority,
    ):
        self.grant_id = grant_id
        self.component = component
        self.granted_bytes = granted_bytes
        self.actual_bytes: int = 0
        self.state = LeaseState.GRANTED
        self.ttl_seconds = ttl_seconds
        self.created_at = time.monotonic()
        self._authority = authority

    async def commit(self, actual_bytes: int) -> None:
        if self.state != LeaseState.GRANTED:
            raise RuntimeError(f"Cannot commit grant in state {self.state}")
        self.actual_bytes = actual_bytes
        self.state = LeaseState.ACTIVE
        self._authority._update_grant(self)

    async def rollback(self, reason: str = "") -> None:
        if self.state in (LeaseState.RELEASED, LeaseState.ROLLED_BACK):
            return
        prev = self.state
        self.state = LeaseState.ROLLED_BACK
        self._authority._release_grant(self)
        logger.info(f"[VRAMAuthority] Grant {self.grant_id} rolled back from {prev.value}: {reason}")

    async def release(self) -> None:
        if self.state in (LeaseState.RELEASED, LeaseState.ROLLED_BACK):
            return
        self.state = LeaseState.RELEASED
        self._authority._release_grant(self)

    async def heartbeat(self) -> None:
        self.created_at = time.monotonic()


class VRAMBudgetAuthority:
    """Lightweight VRAM admission controller for GCP VM."""

    def __init__(self, total_vram_bytes: int):
        self._total = total_vram_bytes
        self._grants: Dict[str, VRAMGrant] = {}
        self._lock = asyncio.Lock()

    @property
    def total_vram_bytes(self) -> int:
        return self._total

    @property
    def allocated_bytes(self) -> int:
        return sum(
            g.granted_bytes for g in self._grants.values()
            if g.state in (LeaseState.GRANTED, LeaseState.ACTIVE)
        )

    @property
    def available_bytes(self) -> int:
        return self._total - self.allocated_bytes

    async def request(
        self,
        component: str,
        bytes_requested: int,
        priority: VRAMPriority,
        *,
        ttl_seconds: float = 300.0,
        releasing_grant_id: Optional[str] = None,
    ) -> Optional[VRAMGrant]:
        """Issue or deny a VRAM grant.

        Args:
            releasing_grant_id: If set, the budget calculation assumes this
                grant will be released during CUTOVER (two-grant swap
                reservation). Without this, same-size model swaps would
                always be denied because both grants count against budget.
        """
        async with self._lock:
            available = self.available_bytes
            # Account for grant being released during swap
            if releasing_grant_id and releasing_grant_id in self._grants:
                releasing = self._grants[releasing_grant_id]
                if releasing.state in (LeaseState.GRANTED, LeaseState.ACTIVE):
                    available += releasing.granted_bytes
            if bytes_requested > available:
                logger.warning(
                    f"[VRAMAuthority] Denied {component}: "
                    f"requested {bytes_requested:,} > available {available:,}"
                )
                return None
            grant = VRAMGrant(
                grant_id=f"vram-{uuid.uuid4().hex[:8]}",
                component=component,
                granted_bytes=bytes_requested,
                ttl_seconds=ttl_seconds,
                authority=self,
            )
            self._grants[grant.grant_id] = grant
            logger.info(
                f"[VRAMAuthority] Granted {grant.grant_id} to {component}: "
                f"{bytes_requested:,} bytes"
            )
            return grant

    def _update_grant(self, grant: VRAMGrant) -> None:
        pass  # Grant already tracked

    def _release_grant(self, grant: VRAMGrant) -> None:
        self._grants.pop(grant.grant_id, None)


# =============================================================================
# TRANSITION STATE MACHINE
# =============================================================================

class TransitionState(Enum):
    IDLE = "idle"
    PREPARE = "prepare"
    DRAIN = "drain"
    CUTOVER = "cutover"
    VERIFY = "verify"
    COMMIT = "commit"
    ROLLBACK = "rollback"


@dataclass
class TransitionEpoch:
    model_epoch: int = 0
    cache_epoch: int = 0
    inventory_epoch: int = 0

    def advance_model(self) -> int:
        self.model_epoch += 1
        return self.model_epoch

    def advance_cache(self) -> int:
        self.cache_epoch += 1
        return self.cache_epoch


@dataclass
class TransitionPolicy:
    min_cooldown_s: float = field(default_factory=lambda: _env_float("JARVIS_MODEL_SWAP_COOLDOWN_S", 90.0))
    max_swaps_per_hour: int = field(default_factory=lambda: _env_int("JARVIS_MODEL_MAX_SWAPS_PER_HOUR", 4))
    quality_dead_zone: float = field(default_factory=lambda: _env_float("JARVIS_MODEL_QUALITY_DEAD_ZONE", 0.05))
    upgrade_sustained_s: float = 30.0
    downgrade_sustained_s: float = 10.0
    cold_start_lockout_s: float = 120.0
    backoff_base_s: float = 90.0
    backoff_multiplier: float = 2.0
    backoff_max_s: float = 600.0


@dataclass(frozen=True)
class ModelTransitionEvent:
    event_type: str
    transition_id: str
    trigger: str
    from_model: Optional[str]
    to_model: str
    from_quant: Optional[str]
    to_quant: str
    model_epoch: int
    duration_ms: Optional[float]
    outcome: str
    reason: str
    timestamp: float


class ModelTransitionManager:
    """THE single executor for all model transitions."""

    def __init__(
        self,
        executor: Any,  # LlamaCppExecutor (not typed to avoid circular import)
        vram_authority: VRAMBudgetAuthority,
        model_dir: Path,
        policy: Optional[TransitionPolicy] = None,
    ):
        self._executor = executor
        self._vram_authority = vram_authority
        self._model_dir = model_dir
        self._policy = policy or TransitionPolicy()
        self._state = TransitionState.IDLE
        self._epoch = TransitionEpoch()
        self._lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._last_swap_time: float = 0.0
        self._swap_times: List[float] = []
        self._current_model_path: Optional[Path] = None
        self._current_grant: Optional[VRAMGrant] = None
        self._active_requests: int = 0
        self._drain_event: Optional[asyncio.Event] = None
        self._event_callbacks: List[Callable[[ModelTransitionEvent], None]] = []
        self._current_fitness: Optional[float] = None

    @property
    def state(self) -> TransitionState:
        return self._state

    @property
    def epoch(self) -> TransitionEpoch:
        return self._epoch

    @property
    def current_model_path(self) -> Optional[Path]:
        return self._current_model_path

    def on_transition_event(self, cb: Callable[[ModelTransitionEvent], None]) -> None:
        self._event_callbacks.append(cb)

    def _emit_event(self, event: ModelTransitionEvent) -> None:
        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception:
                logger.exception("[TransitionManager] Event callback error")

    def _check_cooldown(self) -> Optional[str]:
        """Check if swap is allowed by policy. Returns rejection reason or None."""
        now = time.monotonic()

        # Cold start lockout
        if now - self._started_at < self._policy.cold_start_lockout_s:
            remaining = self._policy.cold_start_lockout_s - (now - self._started_at)
            return f"Cold start lockout: {remaining:.0f}s remaining"

        # Cooldown
        if self._last_swap_time > 0:
            elapsed = now - self._last_swap_time
            cooldown = min(
                self._policy.backoff_base_s * (self._policy.backoff_multiplier ** max(0, len(self._swap_times) - 1)),
                self._policy.backoff_max_s,
            )
            if elapsed < cooldown:
                return f"Cooldown: {cooldown - elapsed:.0f}s remaining"

        # Hourly cap
        hour_ago = now - 3600
        recent = [t for t in self._swap_times if t > hour_ago]
        if len(recent) >= self._policy.max_swaps_per_hour:
            return f"Hourly cap reached: {len(recent)}/{self._policy.max_swaps_per_hour}"

        return None

    async def accept(self, proposal: Any) -> bool:
        """Accept a ModelSelectionProposal and execute the transition."""
        async with self._lock:
            if self._state != TransitionState.IDLE:
                logger.warning(f"[TransitionManager] Rejected: state is {self._state.value}")
                return False

            # Check cooldown (skip for startup trigger)
            if proposal.trigger != "startup":
                rejection = self._check_cooldown()
                if rejection:
                    logger.info(f"[TransitionManager] Rejected: {rejection}")
                    return False

                # Quality dead zone: don't swap if <5% fitness improvement
                if (self._current_fitness is not None and
                    hasattr(proposal, 'quality_score') and
                    abs(proposal.quality_score.fitness_score - self._current_fitness) < self._policy.quality_dead_zone):
                    logger.info("[TransitionManager] Rejected: within quality dead zone")
                    return False

            transition_id = f"trans-{uuid.uuid4().hex[:8]}"
            start_time = time.monotonic()
            target_path = proposal.selected_variant.path
            target_size = proposal.selected_variant.size_bytes
            old_path = self._current_model_path
            old_grant = self._current_grant

            try:
                # PREPARE — use releasing_grant_id for two-grant swap reservation
                self._state = TransitionState.PREPARE
                releasing_id = self._current_grant.grant_id if self._current_grant else None
                new_grant = await self._vram_authority.request(
                    f"model-{proposal.selected_variant.quant_name}",
                    target_size,
                    VRAMPriority.NORMAL,
                    releasing_grant_id=releasing_id,
                )
                if new_grant is None:
                    self._state = TransitionState.IDLE
                    self._emit_event(ModelTransitionEvent(
                        event_type="transition_failed", transition_id=transition_id,
                        trigger=proposal.trigger, from_model=str(old_path),
                        to_model=str(target_path), from_quant=None,
                        to_quant=proposal.selected_variant.quant_name,
                        model_epoch=self._epoch.model_epoch,
                        duration_ms=(time.monotonic() - start_time) * 1000,
                        outcome="denied", reason="VRAM budget denied",
                        timestamp=time.time(),
                    ))
                    return False

                # DRAIN
                self._state = TransitionState.DRAIN
                if self._active_requests > 0:
                    self._drain_event = asyncio.Event()
                    try:
                        await asyncio.wait_for(self._drain_event.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.warning("[TransitionManager] Drain timeout, proceeding with rollback")
                        await new_grant.rollback("drain timeout")
                        self._state = TransitionState.IDLE
                        return False

                # CUTOVER
                self._state = TransitionState.CUTOVER
                if self._executor.is_loaded():
                    await self._executor.unload()
                if old_grant:
                    await old_grant.release()

                await self._executor.load(target_path)
                await new_grant.commit(target_size)

                # VERIFY
                self._state = TransitionState.VERIFY
                valid = await self._executor.validate()
                if not valid:
                    raise RuntimeError("Post-swap validation failed")

                # COMMIT
                self._state = TransitionState.COMMIT
                new_epoch = self._epoch.advance_model()
                self._current_model_path = target_path
                self._current_grant = new_grant
                self._current_fitness = proposal.quality_score.fitness_score
                self._last_swap_time = time.monotonic()
                self._swap_times.append(time.monotonic())

                duration_ms = (time.monotonic() - start_time) * 1000
                self._emit_event(ModelTransitionEvent(
                    event_type="transition_completed", transition_id=transition_id,
                    trigger=proposal.trigger, from_model=str(old_path),
                    to_model=str(target_path), from_quant=None,
                    to_quant=proposal.selected_variant.quant_name,
                    model_epoch=new_epoch, duration_ms=duration_ms,
                    outcome="commit",
                    reason=proposal.reason, timestamp=time.time(),
                ))
                logger.info(
                    f"[TransitionManager] COMMIT epoch={new_epoch} "
                    f"model={target_path.name} in {duration_ms:.0f}ms"
                )
                self._state = TransitionState.IDLE
                return True

            except Exception as e:
                # ROLLBACK
                logger.error(f"[TransitionManager] ROLLBACK: {e}")
                self._state = TransitionState.ROLLBACK
                try:
                    if 'new_grant' in locals() and new_grant:
                        await new_grant.rollback(str(e))
                    if old_path and old_path.exists():
                        await self._executor.load(old_path)
                        # Re-acquire grant for old model
                        restored_grant = await self._vram_authority.request(
                            "model-restored", old_path.stat().st_size, VRAMPriority.CRITICAL,
                        )
                        self._current_grant = restored_grant
                except Exception as rollback_err:
                    logger.critical(f"[TransitionManager] Rollback failed: {rollback_err}")

                self._emit_event(ModelTransitionEvent(
                    event_type="transition_failed", transition_id=transition_id,
                    trigger=proposal.trigger, from_model=str(old_path),
                    to_model=str(target_path), from_quant=None,
                    to_quant=proposal.selected_variant.quant_name,
                    model_epoch=self._epoch.model_epoch,
                    duration_ms=(time.monotonic() - start_time) * 1000,
                    outcome="rollback", reason=str(e), timestamp=time.time(),
                ))
                self._state = TransitionState.IDLE
                return False

    def request_started(self) -> None:
        self._active_requests += 1

    def request_completed(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        if self._active_requests == 0 and self._drain_event:
            self._drain_event.set()

    def status(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "model_epoch": self._epoch.model_epoch,
            "cache_epoch": self._epoch.cache_epoch,
            "current_model": str(self._current_model_path) if self._current_model_path else None,
            "active_requests": self._active_requests,
            "swap_count": len(self._swap_times),
        }
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_model_transition_manager.py::TestVRAMBudgetAuthority -v`
Expected: All tests PASS

- [ ] **Step 5: Write FSM transition tests**

Append to `tests/test_model_transition_manager.py`:

```python
class TestTransitionEpoch:

    def test_advance_model_increments(self):
        epoch = TransitionEpoch()
        assert epoch.model_epoch == 0
        val = epoch.advance_model()
        assert val == 1
        assert epoch.model_epoch == 1

    def test_monotonic(self):
        epoch = TransitionEpoch()
        for i in range(5):
            val = epoch.advance_model()
            assert val == i + 1


class TestTransitionPolicy:

    def test_default_values(self):
        policy = TransitionPolicy()
        assert policy.min_cooldown_s == 90.0
        assert policy.max_swaps_per_hour == 4


class TestModelTransitionManager:

    @pytest.mark.asyncio
    async def test_accept_startup_proposal(self, mock_executor, tmp_models_dir, fake_gguf_files):
        from jarvis_prime.core.adaptive_model_selector import (
            ModelVariant, ModelSelectionProposal, scan_inventory, propose_optimal,
        )

        auth = VRAMBudgetAuthority(total_vram_bytes=23_034 * 1024 * 1024)
        mgr = ModelTransitionManager(
            executor=mock_executor,
            vram_authority=auth,
            model_dir=tmp_models_dir,
        )

        families = await scan_inventory(tmp_models_dir)
        proposal = await propose_optimal(
            families=families,
            vram_budget_bytes=23_034 * 1024 * 1024,
            target_context=8192,
            task_complexity="medium",
            trigger="startup",
        )
        assert proposal is not None
        result = await mgr.accept(proposal)
        assert result is True
        assert mgr.state == TransitionState.IDLE
        assert mgr.epoch.model_epoch == 1

    @pytest.mark.asyncio
    async def test_reject_concurrent_transition(self, mock_executor, tmp_models_dir):
        auth = VRAMBudgetAuthority(total_vram_bytes=23_034 * 1024 * 1024)
        mgr = ModelTransitionManager(
            executor=mock_executor,
            vram_authority=auth,
            model_dir=tmp_models_dir,
        )
        # Force non-IDLE state
        mgr._state = TransitionState.DRAIN
        from unittest.mock import MagicMock
        fake_proposal = MagicMock()
        fake_proposal.trigger = "pressure"
        result = await mgr.accept(fake_proposal)
        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_on_validation_failure(self, mock_executor, tmp_models_dir, fake_gguf_files):
        """Validation failure should trigger ROLLBACK and restore previous model."""
        from jarvis_prime.core.adaptive_model_selector import scan_inventory, propose_optimal

        mock_executor.validate = AsyncMock(return_value=False)
        auth = VRAMBudgetAuthority(total_vram_bytes=23_034 * 1024 * 1024)
        mgr = ModelTransitionManager(
            executor=mock_executor, vram_authority=auth, model_dir=tmp_models_dir,
        )
        families = await scan_inventory(tmp_models_dir)
        proposal = await propose_optimal(
            families=families, vram_budget_bytes=23_034 * 1024 * 1024,
            target_context=8192, task_complexity="medium", trigger="startup",
        )
        result = await mgr.accept(proposal)
        assert result is False
        assert mgr.state == TransitionState.IDLE

    @pytest.mark.asyncio
    async def test_rollback_on_load_failure(self, mock_executor, tmp_models_dir, fake_gguf_files):
        """Load failure should trigger ROLLBACK."""
        from jarvis_prime.core.adaptive_model_selector import scan_inventory, propose_optimal

        mock_executor.load = AsyncMock(side_effect=RuntimeError("OOM"))
        auth = VRAMBudgetAuthority(total_vram_bytes=23_034 * 1024 * 1024)
        mgr = ModelTransitionManager(
            executor=mock_executor, vram_authority=auth, model_dir=tmp_models_dir,
        )
        families = await scan_inventory(tmp_models_dir)
        proposal = await propose_optimal(
            families=families, vram_budget_bytes=23_034 * 1024 * 1024,
            target_context=8192, task_complexity="medium", trigger="startup",
        )
        result = await mgr.accept(proposal)
        assert result is False

    @pytest.mark.asyncio
    async def test_reject_during_cold_start_lockout(self, mock_executor, tmp_models_dir):
        """Swaps should be rejected during cold start lockout."""
        auth = VRAMBudgetAuthority(total_vram_bytes=23_034 * 1024 * 1024)
        mgr = ModelTransitionManager(
            executor=mock_executor, vram_authority=auth, model_dir=tmp_models_dir,
        )
        # Cold start lockout = 120s, just started
        mgr._started_at = time.monotonic()
        from unittest.mock import MagicMock
        fake_proposal = MagicMock()
        fake_proposal.trigger = "pressure"  # Not "startup" — startup bypasses cooldown
        result = await mgr.accept(fake_proposal)
        assert result is False

    @pytest.mark.asyncio
    async def test_swap_reservation_same_size(self):
        """Two-grant swap reservation should allow same-size model swaps."""
        auth = VRAMBudgetAuthority(total_vram_bytes=23_000_000_000)
        g1 = await auth.request("model-a", 11_000_000_000, VRAMPriority.NORMAL)
        assert g1 is not None
        await g1.commit(11_000_000_000)
        # Without releasing_grant_id, this would fail (11+11 > 23)
        g2 = await auth.request(
            "model-b", 11_000_000_000, VRAMPriority.NORMAL,
            releasing_grant_id=g1.grant_id,
        )
        assert g2 is not None
```

- [ ] **Step 6: Run all transition manager tests**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_model_transition_manager.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/model_transition_manager.py tests/test_model_transition_manager.py
git commit -m "feat: implement model transition manager with VRAMBudgetAuthority and FSM"
```

---

## Chunk 6: Quality Regression Tester

### Task 8: Quality Regression Tester

**Files:**
- Create: `jarvis_prime/core/quality_regression_tester.py`
- Create: `tests/test_quality_regression_tester.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for quality_regression_tester.py — A/B benchmarking."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jarvis_prime.core.quality_regression_tester import (
    BenchmarkPrompt,
    BenchmarkSuite,
    BenchmarkResult,
    QualityRegressionTester,
    DEFAULT_SUITE,
)


class TestBenchmarkSuite:

    def test_default_suite_has_prompts(self):
        assert len(DEFAULT_SUITE.prompts) >= 3

    def test_prompts_have_low_temperature(self):
        for p in DEFAULT_SUITE.prompts:
            assert p.temperature <= 0.2


class TestQualityRegressionTester:

    @pytest.mark.asyncio
    async def test_run_benchmark_returns_result(self):
        executor = MagicMock()
        executor.generate = AsyncMock(return_value="def hello():\n    print('hello')\n")
        executor.is_loaded = MagicMock(return_value=True)

        tester = QualityRegressionTester(executor=executor)
        prompt = BenchmarkPrompt(
            name="simple_func",
            prompt="Write a Python hello world function",
            expected_patterns=("def ",),
            max_tokens=50,
        )
        result = await tester.run_single(prompt, model_name="test-model")
        assert result is not None
        assert result.mean_tok_s >= 0
        assert result.quality_score >= 0

    @pytest.mark.asyncio
    async def test_save_and_load_calibration(self, tmp_path):
        cal_dir = tmp_path / "calibration"
        cal_dir.mkdir()

        tester = QualityRegressionTester(
            executor=MagicMock(),
            calibration_dir=cal_dir,
        )
        result = BenchmarkResult(
            variant_name="test-model-Q4_K_M",
            suite_version="1.0",
            mean_tok_s=45.0,
            p50_tok_s=44.5,
            p95_first_token_ms=210.0,
            quality_score=0.92,
            vram_peak_bytes=9_000_000_000,
            context_tested=8192,
            timestamp=1710400000.0,
        )
        tester.save_result("qwen2.5-coder-7b", result)

        loaded = tester.load_calibration("qwen2.5-coder-7b")
        assert loaded is not None
        assert "test-model-Q4_K_M" in loaded.measurements
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quality_regression_tester.py -v 2>&1 | head -10`
Expected: FAIL

- [ ] **Step 3: Implement quality_regression_tester.py**

```python
"""
Quality Regression Tester — A/B Benchmarking
=============================================

Measures quality/speed of quantization variants for calibration data.
Runs asynchronously in background during idle periods.

Preemption rules:
- Always preemptible by production traffic
- Max 30s per prompt
- Results used for calibration only, never for pass/fail gating
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from jarvis_prime.core.quantization_intelligence import CalibrationData, CalibrationPoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BenchmarkPrompt:
    name: str
    prompt: str
    expected_patterns: tuple[str, ...]
    max_tokens: int
    temperature: float = 0.1


@dataclass(frozen=True)
class BenchmarkSuite:
    prompts: tuple[BenchmarkPrompt, ...]
    version: str


@dataclass(frozen=True)
class BenchmarkResult:
    variant_name: str
    suite_version: str
    mean_tok_s: float
    p50_tok_s: float
    p95_first_token_ms: float
    quality_score: float
    vram_peak_bytes: int
    context_tested: int
    timestamp: float


DEFAULT_SUITE = BenchmarkSuite(
    version="1.0",
    prompts=(
        BenchmarkPrompt(
            name="python_function",
            prompt="Write a Python function that checks if a number is prime. Return only the code.",
            expected_patterns=("def ", "return"),
            max_tokens=100,
        ),
        BenchmarkPrompt(
            name="explain_concept",
            prompt="Explain what a binary search tree is in 2-3 sentences.",
            expected_patterns=("tree", "node"),
            max_tokens=80,
        ),
        BenchmarkPrompt(
            name="code_review",
            prompt="Review this code and suggest improvements:\ndef add(a,b): return a+b",
            expected_patterns=("type", "def "),
            max_tokens=100,
        ),
        BenchmarkPrompt(
            name="fibonacci",
            prompt="Write a Python function to compute the nth Fibonacci number iteratively.",
            expected_patterns=("def ", "fib"),
            max_tokens=80,
        ),
    ),
)


class QualityRegressionTester:
    """Background A/B benchmarking for calibration."""

    def __init__(
        self,
        executor: Any = None,
        calibration_dir: Optional[Path] = None,
    ):
        self._executor = executor
        self._calibration_dir = calibration_dir or Path("models/calibration")

    async def run_single(
        self,
        prompt: BenchmarkPrompt,
        model_name: str,
    ) -> Optional[BenchmarkResult]:
        """Run a single benchmark prompt. Preemptible."""
        if not self._executor or not self._executor.is_loaded():
            return None

        start = time.monotonic()
        try:
            output = await self._executor.generate(
                prompt=prompt.prompt,
                max_tokens=prompt.max_tokens,
                temperature=prompt.temperature,
            )
        except Exception as e:
            logger.warning(f"[QualityTester] Benchmark failed: {e}")
            return None

        elapsed = time.monotonic() - start
        tokens_approx = len(output.split()) * 1.3  # rough token estimate
        tok_s = tokens_approx / elapsed if elapsed > 0 else 0

        # Quality: ratio of expected patterns found
        matches = sum(1 for p in prompt.expected_patterns if re.search(p, output, re.IGNORECASE))
        quality = matches / len(prompt.expected_patterns) if prompt.expected_patterns else 1.0

        return BenchmarkResult(
            variant_name=model_name,
            suite_version=DEFAULT_SUITE.version,
            mean_tok_s=tok_s,
            p50_tok_s=tok_s,
            p95_first_token_ms=elapsed * 1000,
            quality_score=quality,
            vram_peak_bytes=0,
            context_tested=prompt.max_tokens,
            timestamp=time.time(),
        )

    def save_result(self, model_family: str, result: BenchmarkResult) -> None:
        """Persist benchmark result to calibration file."""
        self._calibration_dir.mkdir(parents=True, exist_ok=True)
        path = self._calibration_dir / f"{model_family}.json"

        data: Dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        data.setdefault("measurements", {})[result.variant_name] = {
            "measured_tok_s": result.mean_tok_s,
            "measured_perplexity": None,
            "measured_vram_bytes": result.vram_peak_bytes,
            "context_size": result.context_tested,
            "quality_score": result.quality_score,
            "timestamp": result.timestamp,
            "suite_version": result.suite_version,
        }
        path.write_text(json.dumps(data, indent=2))

    def load_calibration(self, model_family: str) -> Optional[CalibrationData]:
        """Load calibration data from disk."""
        path = self._calibration_dir / f"{model_family}.json"
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        measurements: Dict[str, CalibrationPoint] = {}
        for name, m in data.get("measurements", {}).items():
            measurements[name] = CalibrationPoint(
                quant_name=name,
                measured_tok_s=m["measured_tok_s"],
                measured_perplexity=m.get("measured_perplexity"),
                measured_vram_bytes=m["measured_vram_bytes"],
                context_size=m["context_size"],
                timestamp=m["timestamp"],
            )

        return CalibrationData(model_family=model_family, measurements=measurements)
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quality_regression_tester.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/core/quality_regression_tester.py tests/test_quality_regression_tester.py
git commit -m "feat: implement quality regression tester with calibration persistence"
```

---

## Chunk 7: Integration Patches

### Task 9: Extend `/v1/capability` Endpoint

**Files:**
- Modify: `jarvis_prime/server.py` (around line 1562-1650)

- [ ] **Step 1: Read current capability endpoint**

Run: Read `jarvis_prime/server.py` lines 1562-1650

- [ ] **Step 2: Extend capability response with quantization + epoch data**

Add to the capability endpoint's return dict (after existing fields):

```python
    # === Adaptive Quantization Engine extensions (v2.0) ===
    quantization_info = {}
    epoch_info = {}
    pressure_zone = "unknown"

    try:
        from jarvis_prime.core.model_transition_manager import ModelTransitionManager
        # Access transition manager if wired
        _tmgr = getattr(_startup_state, "transition_manager", None) if _startup_state else None
        if _tmgr and isinstance(_tmgr, ModelTransitionManager):
            status = _tmgr.status()
            epoch_info = {
                "model_epoch": status.get("model_epoch", 0),
                "cache_epoch": status.get("cache_epoch", 0),
            }
    except ImportError:
        pass

    try:
        from jarvis_prime.core.vram_pressure_monitor import VRAMPressureMonitor
        _vmon = getattr(_startup_state, "vram_monitor", None) if _startup_state else None
        if _vmon and isinstance(_vmon, VRAMPressureMonitor):
            pressure_zone = _vmon.current_zone.value
    except ImportError:
        pass

    # Merge into response
    response = {
        "schema_version": "2.0",
        "contract_version": "2.0.0",
        # ... existing fields ...
        "quantization": quantization_info,
        "transition_epoch": epoch_info,
        "vram_pressure_zone": pressure_zone,
    }
```

- [ ] **Step 3: Verify server still starts (syntax check)**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -c "import ast; ast.parse(open('jarvis_prime/server.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add jarvis_prime/server.py
git commit -m "feat: extend /v1/capability with quantization engine v2.0 fields"
```

---

### Task 10: Wire Modules into run_server.py Startup

**Files:**
- Modify: `run_server.py`

- [ ] **Step 1: Read current startup flow**

Run: Read `run_server.py` and search for `_load_model` and `_startup_state` to understand wiring points.

- [ ] **Step 2: Add module instantiation to startup**

After the existing model loading code in `run_server.py`, add the adaptive quantization engine wiring. The exact insertion point depends on the current startup flow, but the pattern is:

```python
# === Adaptive Quantization Engine wiring ===
# These are lazy-imported to avoid circular dependencies
try:
    from jarvis_prime.core.vram_pressure_monitor import VRAMPressureMonitor, VRAMMonitorConfig
    from jarvis_prime.core.model_transition_manager import (
        ModelTransitionManager, VRAMBudgetAuthority, TransitionPolicy,
    )
    from jarvis_prime.core.adaptive_model_selector import scan_inventory, propose_optimal

    # Detect total VRAM
    _total_vram = 23_034 * 1024 * 1024  # Default L4
    try:
        import subprocess as _sp
        _nv = _sp.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if _nv.returncode == 0:
            _total_vram = int(float(_nv.stdout.strip()) * 1024 * 1024)
    except Exception:
        pass

    _vram_authority = VRAMBudgetAuthority(total_vram_bytes=_total_vram)
    _vram_monitor = VRAMPressureMonitor(
        config=VRAMMonitorConfig(),
        node_id=os.getenv("JARVIS_HOST_ID", "gcp-jarvis-prime-stable"),
    )
    _transition_manager = ModelTransitionManager(
        executor=_executor,  # The LlamaCppExecutor instance
        vram_authority=_vram_authority,
        model_dir=Path(os.getenv("GCP_MODELS_DIR", "models")),
    )

    # Store on startup state for capability endpoint access
    if _startup_state:
        _startup_state.transition_manager = _transition_manager
        _startup_state.vram_monitor = _vram_monitor

    # Start VRAM monitor background task
    asyncio.create_task(_vram_monitor.start())

    logger.info("[AQE] Adaptive Quantization Engine wired successfully")
except ImportError as e:
    logger.info(f"[AQE] Adaptive Quantization Engine not available: {e}")
except Exception as e:
    logger.warning(f"[AQE] Failed to wire Adaptive Quantization Engine: {e}")
```

**Note:** The exact variable names (`_executor`, `_startup_state`) must match the existing run_server.py code. Read the file first to identify the correct names.

- [ ] **Step 3: Verify syntax**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -c "import ast; ast.parse(open('run_server.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add run_server.py
git commit -m "feat: wire adaptive quantization engine into run_server.py startup"
```

---

### Task 11: Run Full Test Suite

**Note on deferred scope:** The download trust chain (spec Section 3.2.4 — quarantine pipeline, `DownloadProposal`, `DownloadRegistry`) is intentionally deferred to Phase 2. The current plan covers all 6 modules for model selection, scoring, transitions, and monitoring. Download automation will be added after the core engine is validated in production.

- [ ] **Step 1: Run all new tests**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_quantization_intelligence.py tests/test_kv_cache_optimizer.py tests/test_adaptive_model_selector.py tests/test_model_transition_manager.py tests/test_vram_pressure_monitor.py tests/test_quality_regression_tester.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run existing tests to ensure no regressions**

Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/ -v --timeout=60 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 3: Final commit with all files**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add -A
git status
git commit -m "feat: complete adaptive quantization engine — 6 modules + tests + integration"
```
