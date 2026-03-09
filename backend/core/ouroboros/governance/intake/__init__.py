"""Public API for the Unified Intake Layer (Phase 2C)."""
from .intent_envelope import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope,
    SCHEMA_VERSION,
)
from .wal import WAL, WALEntry
from .unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
    RouterAlreadyRunningError,
)

__all__ = [
    "IntentEnvelope",
    "EnvelopeValidationError",
    "make_envelope",
    "SCHEMA_VERSION",
    "WAL",
    "WALEntry",
    "UnifiedIntakeRouter",
    "IntakeRouterConfig",
    "RouterAlreadyRunningError",
]
