"""Public API for the Unified Intake Layer (Phase 2C)."""
from .intent_envelope import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope,
    SCHEMA_VERSION,
)

__all__ = [
    "IntentEnvelope",
    "EnvelopeValidationError",
    "make_envelope",
    "SCHEMA_VERSION",
]
