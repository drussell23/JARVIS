"""ECAPA Budget Bridge — core types for wiring ECAPA voice unlock into Disease 10.

Disease 10 — ECAPA Budget Wiring, Task 1.

Provides the foundational enums, category mapping, and ``BudgetToken`` dataclass
that all subsequent bridge tasks build on.  The token tracks a single ECAPA
operation's lifecycle through the startup concurrency budget system.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from backend.core.startup_concurrency_budget import HeavyTaskCategory

__all__ = [
    "BudgetTokenState",
    "EcapaBudgetRejection",
    "ECAPA_CATEGORY_MAP",
    "BudgetToken",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


@enum.unique
class BudgetTokenState(str, enum.Enum):
    """Lifecycle states of an ECAPA budget token."""

    ACQUIRED = "acquired"
    TRANSFERRED = "transferred"
    REUSED = "reused"
    RELEASED = "released"
    EXPIRED = "expired"


@enum.unique
class EcapaBudgetRejection(str, enum.Enum):
    """Reasons an ECAPA budget acquisition may be rejected."""

    PHASE_BLOCKED = "phase_blocked"
    MEMORY_UNSTABLE = "memory_unstable"
    BUDGET_TIMEOUT = "budget_timeout"
    SLOT_UNAVAILABLE = "slot_unavailable"
    THRASH_EMERGENCY = "thrash_emergency"
    CONTRACT_MISMATCH = "contract_mismatch"


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

ECAPA_CATEGORY_MAP: dict[str, HeavyTaskCategory] = {
    "probe": HeavyTaskCategory.ML_INIT,
    "model_load": HeavyTaskCategory.MODEL_LOAD,
}


# ---------------------------------------------------------------------------
# BudgetToken dataclass
# ---------------------------------------------------------------------------


@dataclass
class BudgetToken:
    """Tracks a single ECAPA operation's budget slot through its lifecycle.

    NOT frozen — ``state`` and timestamp fields must be mutable so the token
    can transition through its lifecycle without creating copies.
    """

    token_id: str
    owner_session_id: str
    state: BudgetTokenState
    category: HeavyTaskCategory
    acquired_at: float
    transferred_at: Optional[float] = None
    released_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None
    token_ttl_s: float = 120.0
    rejection_reason: Optional[EcapaBudgetRejection] = None
    probe_failure_reason: Optional[str] = None
