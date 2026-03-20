# tests/unit/core/test_ecapa_types.py
"""Tests for ECAPA facade types."""
import pytest
import numpy as np


def test_ecapa_state_values():
    from backend.core.ecapa_types import EcapaState
    assert EcapaState.UNINITIALIZED.value == "uninitialized"
    assert EcapaState.READY.value == "ready"
    assert len(EcapaState) == 7


def test_ecapa_tier_values():
    from backend.core.ecapa_types import EcapaTier
    assert len(EcapaTier) == 3


def test_state_to_tier_mapping():
    from backend.core.ecapa_types import EcapaState, EcapaTier, STATE_TO_TIER
    assert STATE_TO_TIER[EcapaState.UNINITIALIZED] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.PROBING] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.LOADING] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.READY] == EcapaTier.READY
    assert STATE_TO_TIER[EcapaState.DEGRADED] == EcapaTier.DEGRADED
    assert STATE_TO_TIER[EcapaState.UNAVAILABLE] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.RECOVERING] == EcapaTier.UNAVAILABLE
    for state in EcapaState:
        assert state in STATE_TO_TIER


def test_config_from_env_defaults(monkeypatch):
    from backend.core.ecapa_types import EcapaFacadeConfig
    for key in ["ECAPA_FAILURE_THRESHOLD", "ECAPA_RECOVERY_THRESHOLD",
                "ECAPA_TRANSITION_COOLDOWN_S", "ECAPA_REPROBE_INTERVAL_S",
                "ECAPA_REPROBE_MAX_BACKOFF_S", "ECAPA_REPROBE_BUDGET",
                "ECAPA_PROBE_TIMEOUT_S", "ECAPA_LOCAL_LOAD_TIMEOUT_S",
                "ECAPA_MAX_CONCURRENT_EXTRACTIONS", "ECAPA_RECOVERING_FAIL_THRESHOLD"]:
        monkeypatch.delenv(key, raising=False)
    cfg = EcapaFacadeConfig.from_env()
    assert cfg.failure_threshold == 3
    assert cfg.recovery_threshold == 3
    assert cfg.probe_timeout_s == 8.0
    assert cfg.max_concurrent_extractions == 4
    assert cfg.recovering_fail_threshold == 2


def test_config_from_env_overrides(monkeypatch):
    from backend.core.ecapa_types import EcapaFacadeConfig
    monkeypatch.setenv("ECAPA_FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("ECAPA_PROBE_TIMEOUT_S", "12.5")
    cfg = EcapaFacadeConfig.from_env()
    assert cfg.failure_threshold == 5
    assert cfg.probe_timeout_s == 12.5


def test_embedding_result_success():
    from backend.core.ecapa_types import EmbeddingResult
    r = EmbeddingResult(
        embedding=np.zeros(192), backend="local",
        latency_ms=50.0, from_cache=False, dimension=192, error=None,
    )
    assert r.success is True


def test_embedding_result_failure():
    from backend.core.ecapa_types import EmbeddingResult
    r = EmbeddingResult(
        embedding=None, backend="local",
        latency_ms=0.0, from_cache=False, dimension=192, error="timeout",
    )
    assert r.success is False


def test_voice_capability_enum():
    from backend.core.ecapa_types import VoiceCapability
    assert VoiceCapability.VOICE_UNLOCK.value == "CAP_VOICE_UNLOCK"
    assert len(VoiceCapability) == 8


def test_ecapa_errors():
    from backend.core.ecapa_types import (
        EcapaError, EcapaUnavailableError, EcapaOverloadError, EcapaTimeoutError,
    )
    assert issubclass(EcapaUnavailableError, EcapaError)
    assert issubclass(EcapaOverloadError, EcapaError)
    assert issubclass(EcapaTimeoutError, EcapaError)
    err = EcapaOverloadError(retry_after_s=2.5)
    assert err.retry_after_s == 2.5
    assert "2.5" in str(err)
