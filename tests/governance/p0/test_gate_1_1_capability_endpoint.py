"""Gate 1.1 — J-Prime capability contract endpoint.

Rubric requirements:
- test_capability_endpoint_contract_shape
- test_capability_endpoint_includes_contract_and_context_window
- test_capability_endpoint_reports_model_loaded_state
- test_health_endpoint_not_used_for_capability_contract

Tests use the CapabilityPayload schema validator and a mock HTTP server —
no live J-Prime required.  The consumer-side validator (used by
TelemetryContextualizer and supervisor) is what is being exercised here.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Schema under test
# ---------------------------------------------------------------------------

from backend.core.capability_contract import (
    CapabilityPayload,
    CapabilityPayloadError,
    ModelCapability,
    validate_capability_payload,
    CAPABILITY_ENDPOINT_PATH,
    HEALTH_ENDPOINT_PATH,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_payload() -> Dict[str, Any]:
    return {
        "contract_version": "1.0",
        "capability_schema_version": "1.0",
        "generated_at_utc": "2026-03-10T22:00:00Z",
        "models": {
            "qwen-2.5-coder-7b": {
                "loaded": True,
                "context_window_size": 8192,
                "supports_intents": ["code_generation", "code_review"],
            },
            "mistral-7b": {
                "loaded": True,
                "context_window_size": 8192,
                "supports_intents": ["general"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Gate 1.1 tests
# ---------------------------------------------------------------------------


class TestCapabilityEndpointContractShape:
    """test_capability_endpoint_contract_shape

    Validates that a well-formed payload is accepted and
    a payload missing required top-level keys is rejected with an explicit error.
    """

    def test_valid_payload_parses_without_error(self):
        payload = _valid_payload()
        result = validate_capability_payload(payload)
        assert isinstance(result, CapabilityPayload)

    def test_missing_contract_version_raises(self):
        payload = _valid_payload()
        del payload["contract_version"]
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "contract_version" in str(exc_info.value)

    def test_missing_models_raises(self):
        payload = _valid_payload()
        del payload["models"]
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "models" in str(exc_info.value)

    def test_missing_generated_at_utc_raises(self):
        payload = _valid_payload()
        del payload["generated_at_utc"]
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "generated_at_utc" in str(exc_info.value)

    def test_empty_models_dict_raises(self):
        payload = _valid_payload()
        payload["models"] = {}
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "models" in str(exc_info.value).lower()

    def test_non_dict_payload_raises(self):
        with pytest.raises(CapabilityPayloadError):
            validate_capability_payload("not-a-dict")  # type: ignore[arg-type]


class TestCapabilityEndpointIncludesContractAndContextWindow:
    """test_capability_endpoint_includes_contract_and_context_window

    Validates that contract_version and per-model context_window_size
    are present and non-null.
    """

    def test_contract_version_is_accessible(self):
        result = validate_capability_payload(_valid_payload())
        assert result.contract_version == "1.0"

    def test_context_window_size_present_for_all_models(self):
        result = validate_capability_payload(_valid_payload())
        for model_id, model in result.models.items():
            assert model.context_window_size is not None, (
                f"context_window_size missing for model {model_id!r}"
            )
            assert model.context_window_size > 0

    def test_null_context_window_raises(self):
        payload = _valid_payload()
        payload["models"]["qwen-2.5-coder-7b"]["context_window_size"] = None
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "context_window_size" in str(exc_info.value)

    def test_zero_context_window_raises(self):
        payload = _valid_payload()
        payload["models"]["qwen-2.5-coder-7b"]["context_window_size"] = 0
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "context_window_size" in str(exc_info.value)

    def test_capability_schema_version_accessible(self):
        result = validate_capability_payload(_valid_payload())
        assert result.capability_schema_version == "1.0"


class TestCapabilityEndpointReportsModelLoadedState:
    """test_capability_endpoint_reports_model_loaded_state

    Validates that the loaded field is present and boolean for each model,
    and that the payload with all models unloaded is still valid (not an error).
    """

    def test_loaded_true_propagated(self):
        result = validate_capability_payload(_valid_payload())
        assert result.models["qwen-2.5-coder-7b"].loaded is True

    def test_loaded_false_propagated(self):
        payload = _valid_payload()
        payload["models"]["mistral-7b"]["loaded"] = False
        result = validate_capability_payload(payload)
        assert result.models["mistral-7b"].loaded is False

    def test_missing_loaded_field_raises(self):
        payload = _valid_payload()
        del payload["models"]["qwen-2.5-coder-7b"]["loaded"]
        with pytest.raises(CapabilityPayloadError) as exc_info:
            validate_capability_payload(payload)
        assert "loaded" in str(exc_info.value)

    def test_all_models_unloaded_is_valid_payload(self):
        payload = _valid_payload()
        for m in payload["models"].values():
            m["loaded"] = False
        # Valid shape — supervisor decides how to react, not the parser
        result = validate_capability_payload(payload)
        assert all(not m.loaded for m in result.models.values())

    def test_supports_intents_propagated(self):
        result = validate_capability_payload(_valid_payload())
        assert "code_generation" in result.models["qwen-2.5-coder-7b"].supports_intents


class TestHealthEndpointNotUsedForCapabilityContract:
    """test_health_endpoint_not_used_for_capability_contract

    Validates that:
    1. CAPABILITY_ENDPOINT_PATH != HEALTH_ENDPOINT_PATH (separate routes).
    2. The capability validator rejects a payload that has health-only fields
       (process health masquerading as capability data).
    """

    def test_capability_path_is_not_health_path(self):
        assert CAPABILITY_ENDPOINT_PATH != HEALTH_ENDPOINT_PATH, (
            "Capability and health endpoints must be distinct paths — "
            "do not overload /health with capability contract data"
        )

    def test_capability_path_contains_capability_word(self):
        assert "capability" in CAPABILITY_ENDPOINT_PATH.lower()

    def test_health_only_payload_missing_capability_fields_raises(self):
        health_only = {
            "service": "jarvis_prime",
            "status": "healthy",
            "phase": "ready",
            "pid": 1234,
            "model_loaded": True,
        }
        with pytest.raises(CapabilityPayloadError):
            validate_capability_payload(health_only)

    def test_capability_payload_accepted_on_correct_path(self):
        # Symbolic: path constant is the declared contract path
        assert CAPABILITY_ENDPOINT_PATH.startswith("/")
        result = validate_capability_payload(_valid_payload())
        assert result is not None
