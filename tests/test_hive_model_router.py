"""Tests for backend.hive.model_router — cognitive-state-aware model selection."""

from __future__ import annotations

import pytest

from backend.hive.model_router import HiveModelRouter
from backend.hive.thread_models import CognitiveState


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def router() -> HiveModelRouter:
    """Return a router initialised with default env (no overrides)."""
    return HiveModelRouter()


# ------------------------------------------------------------------
# get_model
# ------------------------------------------------------------------


class TestGetModel:
    def test_baseline_returns_none(self, router: HiveModelRouter) -> None:
        assert router.get_model(CognitiveState.BASELINE) is None

    def test_rem_returns_35b(self, router: HiveModelRouter) -> None:
        model = router.get_model(CognitiveState.REM)
        assert model == "Qwen/Qwen3.5-35B-A3B-FP8"

    def test_flow_returns_397b(self, router: HiveModelRouter) -> None:
        model = router.get_model(CognitiveState.FLOW)
        assert model == "Qwen/Qwen3.5-397B-A17B-FP8"


# ------------------------------------------------------------------
# embedding_model property
# ------------------------------------------------------------------


class TestEmbeddingModel:
    def test_embedding_model_default(self, router: HiveModelRouter) -> None:
        assert router.embedding_model == "Qwen/Qwen3-Embedding-8B"


# ------------------------------------------------------------------
# env override
# ------------------------------------------------------------------


class TestEnvOverride:
    def test_env_override_rem_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_HIVE_REM_MODEL", "custom/my-rem-model")
        overridden = HiveModelRouter()
        assert overridden.get_model(CognitiveState.REM) == "custom/my-rem-model"

    def test_env_override_flow_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_HIVE_FLOW_MODEL", "custom/my-flow-model")
        overridden = HiveModelRouter()
        assert overridden.get_model(CognitiveState.FLOW) == "custom/my-flow-model"

    def test_env_override_embedding_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_HIVE_EMBEDDING_MODEL", "custom/my-embed")
        overridden = HiveModelRouter()
        assert overridden.embedding_model == "custom/my-embed"


# ------------------------------------------------------------------
# get_config
# ------------------------------------------------------------------


class TestGetConfig:
    def test_baseline_config(self, router: HiveModelRouter) -> None:
        cfg = router.get_config(CognitiveState.BASELINE)
        assert cfg["model"] is None
        assert cfg["max_tokens"] == 0
        assert cfg["temperature"] == 0

    def test_rem_config(self, router: HiveModelRouter) -> None:
        cfg = router.get_config(CognitiveState.REM)
        assert cfg["model"] == "Qwen/Qwen3.5-35B-A3B-FP8"
        assert cfg["max_tokens"] == 4000
        assert cfg["temperature"] == 0.3

    def test_flow_config(self, router: HiveModelRouter) -> None:
        cfg = router.get_config(CognitiveState.FLOW)
        assert cfg["model"] == "Qwen/Qwen3.5-397B-A17B-FP8"
        assert cfg["max_tokens"] == 10000
        assert cfg["temperature"] == 0.2

    def test_config_has_all_keys(self, router: HiveModelRouter) -> None:
        for state in CognitiveState:
            cfg = router.get_config(state)
            assert "model" in cfg
            assert "max_tokens" in cfg
            assert "temperature" in cfg
