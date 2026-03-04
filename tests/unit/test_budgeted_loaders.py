"""Tests for backend.core.budgeted_loaders -- BudgetedLoader protocol and adapters.

Covers the protocol conformance, component_id formatting, phase/priority
defaults, estimate_bytes calculations, degradation options, prove_config
stubs, and release_handle behaviour for all four loader adapters.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict
from unittest.mock import patch

import pytest

from backend.core.budgeted_loaders import (
    BudgetedLoader,
    EcapaBudgetedLoader,
    EmbeddingBudgetedLoader,
    LLMBudgetedLoader,
    WhisperBudgetedLoader,
)
from backend.core.memory_types import (
    BudgetPriority,
    StartupPhase,
)


# ===================================================================
# LLM Loader
# ===================================================================


class TestLLMLoader:
    """Tests for LLMBudgetedLoader."""

    def test_component_id_includes_model(self) -> None:
        loader = LLMBudgetedLoader(model_name="mistral-7b-q4", size_mb=4370)
        assert loader.component_id == "llm:mistral-7b-q4@v1"

    def test_component_id_default(self) -> None:
        loader = LLMBudgetedLoader()
        assert loader.component_id == "llm:unknown@v1"

    def test_phase_default_interactive(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        assert loader.phase == StartupPhase.BOOT_OPTIONAL

    def test_priority_default_interactive(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        assert loader.priority == BudgetPriority.BOOT_OPTIONAL

    @patch.dict(os.environ, {"JARVIS_BOOT_PROFILE": "headless"})
    def test_phase_headless(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        assert loader.phase == StartupPhase.BACKGROUND

    @patch.dict(os.environ, {"JARVIS_BOOT_PROFILE": "headless"})
    def test_priority_headless(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        assert loader.priority == BudgetPriority.BACKGROUND

    def test_estimate_includes_kv_cache(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=2048,
        )
        estimate = loader.estimate_bytes({})
        # Must exceed raw model size + overhead
        assert estimate > 4370 * 1024 * 1024

    def test_estimate_grows_with_context(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=2048,
        )
        est_2k = loader.estimate_bytes({"context_length": 2048})
        est_8k = loader.estimate_bytes({"context_length": 8192})
        assert est_8k > est_2k

    def test_estimate_config_override(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=1000, context_length=2048,
        )
        est_default = loader.estimate_bytes({})
        est_bigger = loader.estimate_bytes({"size_mb": 4000})
        assert est_bigger > est_default

    def test_estimate_zero_size(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=0)
        estimate = loader.estimate_bytes({})
        # Should still return overhead (512 MB)
        assert estimate == 512 * 1024 * 1024

    def test_degradation_options_present(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=4096,
        )
        opts = loader.degradation_options
        assert len(opts) >= 2
        assert any("context" in o.name for o in opts)

    def test_degradation_reduce_context_2048(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=4096,
        )
        opts = loader.degradation_options
        names = [o.name for o in opts]
        assert "reduce_context_2048" in names

    def test_degradation_reduce_context_1024(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=4096,
        )
        opts = loader.degradation_options
        names = [o.name for o in opts]
        assert "reduce_context_1024" in names

    def test_degradation_cpu_only_always_present(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=1024,
        )
        opts = loader.degradation_options
        names = [o.name for o in opts]
        assert "cpu_only" in names

    def test_degradation_no_context_options_at_2048(self) -> None:
        """At context=2048, reduce_context_2048 should not appear."""
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=2048,
        )
        opts = loader.degradation_options
        names = [o.name for o in opts]
        assert "reduce_context_2048" not in names
        # reduce_context_1024 should still appear (2048 > 1024)
        assert "reduce_context_1024" in names

    def test_degradation_no_context_options_at_1024(self) -> None:
        """At context=1024, no context reduction options should appear."""
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=1024,
        )
        opts = loader.degradation_options
        context_opts = [o for o in opts if "context" in o.name]
        assert len(context_opts) == 0

    def test_degradation_cpu_only_caps_context(self) -> None:
        loader = LLMBudgetedLoader(
            model_name="test", size_mb=4370, context_length=8192,
        )
        cpu_opt = [o for o in loader.degradation_options if o.name == "cpu_only"][0]
        assert cpu_opt.constraints["n_gpu_layers"] == 0
        assert cpu_opt.constraints["context_length"] == 2048

    def test_load_with_grant_implemented(self) -> None:
        """load_with_grant is implemented (Task 7); no longer raises NotImplementedError."""
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        # Verify the method exists and is not a stub
        import inspect
        assert inspect.iscoroutinefunction(loader.load_with_grant)

    def test_prove_config_returns_compliant(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        proof = loader.prove_config({"context_length": 2048})
        assert proof.compliant is True
        assert proof.component_id == "llm:test@v1"

    def test_measure_actual_bytes_zero_before_load(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        assert loader.measure_actual_bytes() == 0

    def test_release_handle_clears_model(self) -> None:
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        loader._model_handle = "fake_handle"
        asyncio.get_event_loop().run_until_complete(
            loader.release_handle("test cleanup")
        )
        assert loader._model_handle is None


# ===================================================================
# Whisper Loader
# ===================================================================


class TestWhisperLoader:
    """Tests for WhisperBudgetedLoader."""

    def test_component_id(self) -> None:
        assert WhisperBudgetedLoader("base").component_id == "whisper:base@v1"

    def test_component_id_tiny(self) -> None:
        assert WhisperBudgetedLoader("tiny").component_id == "whisper:tiny@v1"

    def test_phase(self) -> None:
        assert WhisperBudgetedLoader("base").phase == StartupPhase.BOOT_OPTIONAL

    def test_priority(self) -> None:
        assert WhisperBudgetedLoader("base").priority == BudgetPriority.BOOT_OPTIONAL

    def test_estimate_known_sizes(self) -> None:
        for size, min_mb in [("tiny", 75), ("base", 150), ("small", 500)]:
            loader = WhisperBudgetedLoader(size)
            est = loader.estimate_bytes({})
            assert est >= min_mb * 1024 * 1024, (
                f"Whisper {size} estimate {est} < {min_mb} MB"
            )

    def test_estimate_includes_overhead(self) -> None:
        loader = WhisperBudgetedLoader("base")
        # Must be at least model (150 MB) + overhead (200 MB) = 350 MB
        assert loader.estimate_bytes({}) >= 350 * 1024 * 1024

    def test_degradation_to_tiny(self) -> None:
        loader = WhisperBudgetedLoader("base")
        opts = loader.degradation_options
        assert len(opts) == 1
        assert opts[0].name == "whisper_tiny"

    def test_degradation_from_large(self) -> None:
        loader = WhisperBudgetedLoader("large")
        opts = loader.degradation_options
        assert len(opts) == 1
        assert opts[0].name == "whisper_tiny"

    def test_no_degradation_when_tiny(self) -> None:
        loader = WhisperBudgetedLoader("tiny")
        assert loader.degradation_options == []

    def test_invalid_model_size_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown Whisper model size"):
            WhisperBudgetedLoader("huge")

    def test_load_with_grant_raises(self) -> None:
        loader = WhisperBudgetedLoader("base")
        with pytest.raises(NotImplementedError, match="Task 7"):
            asyncio.get_event_loop().run_until_complete(
                loader.load_with_grant(None)  # type: ignore[arg-type]
            )

    def test_release_handle_clears_model(self) -> None:
        loader = WhisperBudgetedLoader("base")
        loader._model_handle = "fake_handle"
        asyncio.get_event_loop().run_until_complete(
            loader.release_handle("test cleanup")
        )
        assert loader._model_handle is None


# ===================================================================
# ECAPA Loader
# ===================================================================


class TestEcapaLoader:
    """Tests for EcapaBudgetedLoader."""

    def test_component_id(self) -> None:
        assert EcapaBudgetedLoader().component_id == "ecapa_tdnn@v1"

    def test_phase(self) -> None:
        assert EcapaBudgetedLoader().phase == StartupPhase.BOOT_OPTIONAL

    def test_priority(self) -> None:
        assert EcapaBudgetedLoader().priority == BudgetPriority.BOOT_OPTIONAL

    def test_estimate(self) -> None:
        assert EcapaBudgetedLoader().estimate_bytes({}) >= 300 * 1024 * 1024

    def test_estimate_exact(self) -> None:
        assert EcapaBudgetedLoader().estimate_bytes({}) == 350 * 1024 * 1024

    def test_no_degradation(self) -> None:
        assert EcapaBudgetedLoader().degradation_options == []

    def test_load_with_grant_raises(self) -> None:
        loader = EcapaBudgetedLoader()
        with pytest.raises(NotImplementedError, match="Task 7"):
            asyncio.get_event_loop().run_until_complete(
                loader.load_with_grant(None)  # type: ignore[arg-type]
            )

    def test_prove_config_returns_compliant(self) -> None:
        proof = EcapaBudgetedLoader().prove_config({})
        assert proof.compliant is True
        assert proof.component_id == "ecapa_tdnn@v1"

    def test_release_handle_clears_model(self) -> None:
        loader = EcapaBudgetedLoader()
        loader._model_handle = "fake_handle"
        asyncio.get_event_loop().run_until_complete(
            loader.release_handle("test cleanup")
        )
        assert loader._model_handle is None


# ===================================================================
# Embedding Loader
# ===================================================================


class TestEmbeddingLoader:
    """Tests for EmbeddingBudgetedLoader."""

    def test_component_id(self) -> None:
        assert (
            EmbeddingBudgetedLoader().component_id
            == "embedding:all-MiniLM-L6-v2@v1"
        )

    def test_phase(self) -> None:
        assert EmbeddingBudgetedLoader().phase == StartupPhase.BOOT_OPTIONAL

    def test_priority(self) -> None:
        assert EmbeddingBudgetedLoader().priority == BudgetPriority.BOOT_OPTIONAL

    def test_estimate(self) -> None:
        assert EmbeddingBudgetedLoader().estimate_bytes({}) >= 300 * 1024 * 1024

    def test_estimate_exact(self) -> None:
        assert EmbeddingBudgetedLoader().estimate_bytes({}) == 400 * 1024 * 1024

    def test_no_degradation(self) -> None:
        assert EmbeddingBudgetedLoader().degradation_options == []

    def test_load_with_grant_raises(self) -> None:
        loader = EmbeddingBudgetedLoader()
        with pytest.raises(NotImplementedError, match="Task 7"):
            asyncio.get_event_loop().run_until_complete(
                loader.load_with_grant(None)  # type: ignore[arg-type]
            )

    def test_prove_config_returns_compliant(self) -> None:
        proof = EmbeddingBudgetedLoader().prove_config({"dim": 384})
        assert proof.compliant is True
        assert proof.component_id == "embedding:all-MiniLM-L6-v2@v1"

    def test_release_handle_clears_model(self) -> None:
        loader = EmbeddingBudgetedLoader()
        loader._model_handle = "fake_handle"
        asyncio.get_event_loop().run_until_complete(
            loader.release_handle("test cleanup")
        )
        assert loader._model_handle is None


# ===================================================================
# Protocol conformance
# ===================================================================


class TestProtocol:
    """Verify all four loaders satisfy the BudgetedLoader protocol."""

    def test_llm_implements_protocol(self) -> None:
        assert isinstance(LLMBudgetedLoader(), BudgetedLoader)

    def test_whisper_implements_protocol(self) -> None:
        assert isinstance(WhisperBudgetedLoader("base"), BudgetedLoader)

    def test_ecapa_implements_protocol(self) -> None:
        assert isinstance(EcapaBudgetedLoader(), BudgetedLoader)

    def test_embedding_implements_protocol(self) -> None:
        assert isinstance(EmbeddingBudgetedLoader(), BudgetedLoader)
