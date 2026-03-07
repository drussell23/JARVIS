"""
Ouroboros Governance Integration Module
=======================================

Wires the governance stack (Phases 0-3) into the running JARVIS system.
All governance lifecycle logic lives here. The unified_supervisor.py gets
minimal hook calls at 4 explicit points — this module owns the mechanics.

CONSTRAINT: No side effects on import.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# GovernanceMode
# ---------------------------------------------------------------------------


class GovernanceMode(enum.Enum):
    """Operating modes for the governance stack.

    All mode fields use this enum — no string literals anywhere.
    """

    PENDING = "pending"
    SANDBOX = "sandbox"
    READ_ONLY_PLANNING = "read_only_planning"
    GOVERNED = "governed"
    EMERGENCY_STOP = "emergency_stop"


# ---------------------------------------------------------------------------
# CapabilityStatus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityStatus:
    """Status of an optional governance capability.

    Not just a bool — carries a reason string for degraded boot observability.
    Reasons: "ok", "dep_missing", "init_timeout", "init_error"
    """

    enabled: bool
    reason: str


# ---------------------------------------------------------------------------
# GovernanceInitError
# ---------------------------------------------------------------------------


class GovernanceInitError(Exception):
    """Raised when governance stack creation fails.

    Carries a reason_code for structured logging and observability.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")
