"""Reactive State Propagation -- Disease 8 cure.

Replaces 23+ environment variables used for cross-component state
with a versioned, observable, typed, CAS-protected state store.
"""
from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import PolicyEngine, build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import (
    StateEntry,
    WriteResult,
    WriteStatus,
)

__all__ = [
    "AuditLog",
    "AuditSeverity",
    "PolicyEngine",
    "ReactiveStateStore",
    "StateEntry",
    "WriteResult",
    "WriteStatus",
    "build_default_policy_engine",
    "build_ownership_registry",
    "build_schema_registry",
]
