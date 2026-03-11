"""CapabilityContract — J-Prime capability endpoint schema and validator.

Defines the contract between JARVIS supervisor and J-Prime /capability endpoint.
Kept separate from /health — health reports process liveness, capability reports
model readiness and contract version.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


CAPABILITY_ENDPOINT_PATH = "/capability"
HEALTH_ENDPOINT_PATH = "/health"
EXPECTED_CONTRACT_VERSION = "1.0"


class CapabilityPayloadError(ValueError):
    """Raised when a capability payload is malformed or missing required fields."""


@dataclass(frozen=True)
class ModelCapability:
    """Per-model capability information from J-Prime."""

    loaded: bool
    context_window_size: int
    supports_intents: List[str]


@dataclass(frozen=True)
class CapabilityPayload:
    """Full capability contract payload from J-Prime /capability endpoint."""

    contract_version: str
    capability_schema_version: str
    generated_at_utc: str
    models: Dict[str, ModelCapability]


def validate_capability_payload(raw: Any) -> CapabilityPayload:
    """Validate and parse a raw capability payload dict.

    Raises
    ------
    CapabilityPayloadError
        On any missing or invalid field.
    """
    if not isinstance(raw, dict):
        raise CapabilityPayloadError(
            f"Capability payload must be a dict, got {type(raw).__name__}"
        )

    for field_name in ("contract_version", "capability_schema_version", "generated_at_utc", "models"):
        if field_name not in raw:
            raise CapabilityPayloadError(f"Missing required field: {field_name!r}")

    raw_models = raw["models"]
    if not isinstance(raw_models, dict) or len(raw_models) == 0:
        raise CapabilityPayloadError("Field 'models' must be a non-empty dict")

    models: Dict[str, ModelCapability] = {}
    for model_id, model_data in raw_models.items():
        if not isinstance(model_data, dict):
            raise CapabilityPayloadError(
                f"Model {model_id!r}: expected dict, got {type(model_data).__name__}"
            )

        if "loaded" not in model_data:
            raise CapabilityPayloadError(
                f"Model {model_id!r}: missing required field 'loaded'"
            )

        if "context_window_size" not in model_data:
            raise CapabilityPayloadError(
                f"Model {model_id!r}: missing required field 'context_window_size'"
            )

        ctx_size = model_data["context_window_size"]
        if ctx_size is None:
            raise CapabilityPayloadError(
                f"Model {model_id!r}: context_window_size must not be null"
            )
        if not isinstance(ctx_size, int) or ctx_size <= 0:
            raise CapabilityPayloadError(
                f"Model {model_id!r}: context_window_size must be a positive integer, got {ctx_size!r}"
            )

        models[model_id] = ModelCapability(
            loaded=bool(model_data["loaded"]),
            context_window_size=ctx_size,
            supports_intents=list(model_data.get("supports_intents", [])),
        )

    return CapabilityPayload(
        contract_version=str(raw["contract_version"]),
        capability_schema_version=str(raw["capability_schema_version"]),
        generated_at_utc=str(raw["generated_at_utc"]),
        models=models,
    )
