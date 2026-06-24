"""Sovereign Command Node -- the operator write-path into governance.

Phase 2 ships the Biometric Edge-Gate: an ECAPA-voice-gated
``/authorize-elevation`` write-path for CRITICAL_ELEVATION cross-repo
PRs. The biometric is NECESSARY, never SUFFICIENT -- the existing
CRITICAL_ELEVATION approval path + the Immutable Orange floor compose
UNDERNEATH and CANNOT be weakened by a valid voice match.

Fail-CLOSED absolute. Default-OFF. Audio never persisted.
"""
from __future__ import annotations

__all__ = [
    "biometric_audit_ledger",
    "biometric_auth_middleware",
    "command_node_router",
]
