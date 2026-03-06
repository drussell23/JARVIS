"""ECAPA Budget Bridge — core types and singleton bridge for Disease 10.

Disease 10 — ECAPA Budget Wiring, Tasks 1 & 2.

Provides the foundational enums, category mapping, ``BudgetToken`` dataclass,
and the ``EcapaBudgetBridge`` singleton that manages the full token lifecycle
(acquire → transfer → reuse → release/expire).
"""

from __future__ import annotations

import enum
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from backend.core.startup_concurrency_budget import HeavyTaskCategory

__all__ = [
    "BudgetTokenState",
    "EcapaBudgetRejection",
    "ECAPA_CATEGORY_MAP",
    "BudgetToken",
    "EcapaBudgetBridge",
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


# ---------------------------------------------------------------------------
# Singleton bridge
# ---------------------------------------------------------------------------

_log = logging.getLogger(__name__)


class EcapaBudgetBridge:
    """Process-wide singleton managing ECAPA budget token lifecycles.

    Tracks tokens through ACQUIRED → TRANSFERRED → REUSED → RELEASED/EXPIRED
    and enforces the single-MODEL_LOAD invariant via ``_check_invariant()``.
    """

    _instance: Optional[EcapaBudgetBridge] = None

    # -- Singleton lifecycle ------------------------------------------------

    def _init_internal(self) -> None:
        """Separated init for test isolation — sets up fresh state."""
        self._tokens: dict[str, BudgetToken] = {}
        self._active_model_load_count: int = 0
        self._frozen: bool = False
        self._session_id: str = str(uuid.uuid4())

    @classmethod
    def get_instance(cls) -> EcapaBudgetBridge:
        """Return the process-wide singleton, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance._init_internal()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton — for testing only."""
        cls._instance = None

    # -- Token lifecycle methods --------------------------------------------

    def transfer_token(self, token: BudgetToken) -> BudgetToken:
        """CAS: ACQUIRED → TRANSFERRED.

        Sets ``transferred_at``.  A second call on the same token raises
        ``ValueError`` with ``"CAS"`` in the message.
        """
        if token.state is not BudgetTokenState.ACQUIRED:
            raise ValueError(
                f"CAS violation: expected ACQUIRED, got {token.state.value} "
                f"for token {token.token_id}"
            )
        token.state = BudgetTokenState.TRANSFERRED
        token.transferred_at = time.monotonic()
        return token

    def reuse_token(
        self, token: BudgetToken, requester_session_id: str
    ) -> BudgetToken:
        """TRANSFERRED → REUSED.

        Validates that ``owner_session_id`` matches ``requester_session_id``.
        Raises ``ValueError`` with ``"owner"`` in the message on mismatch.
        Updates ``last_heartbeat_at``.
        """
        if token.owner_session_id != requester_session_id:
            raise ValueError(
                f"Session owner mismatch: token owned by "
                f"'{token.owner_session_id}', requester is "
                f"'{requester_session_id}'"
            )
        token.state = BudgetTokenState.REUSED
        token.last_heartbeat_at = time.monotonic()
        return token

    def heartbeat(self, token: BudgetToken) -> None:
        """Update ``last_heartbeat_at`` to the current monotonic time."""
        token.last_heartbeat_at = time.monotonic()

    def release(self, token: BudgetToken) -> None:
        """Release a token.  Idempotent — no-op if already RELEASED or EXPIRED."""
        if token.state in (BudgetTokenState.RELEASED, BudgetTokenState.EXPIRED):
            return
        token.state = BudgetTokenState.RELEASED
        token.released_at = time.monotonic()
        if token.category is HeavyTaskCategory.MODEL_LOAD:
            self._active_model_load_count = max(
                0, self._active_model_load_count - 1
            )

    def cleanup_expired(
        self,
        max_age_s: float = 120.0,
        heartbeat_silence_s: float = 45.0,
    ) -> int:
        """Expire stale tokens and return the count of newly expired ones.

        Rules:
        - REUSED with stale heartbeat (> ``heartbeat_silence_s``) → EXPIRED.
        - REUSED with fresh heartbeat → preserved.
        - ACQUIRED / TRANSFERRED with no heartbeat + age > ``max_age_s`` → EXPIRED.
        - Decrements ``_active_model_load_count`` for expired MODEL_LOAD tokens.
        """
        now = time.monotonic()
        expired_count = 0

        for token in list(self._tokens.values()):
            should_expire = False

            if token.state is BudgetTokenState.REUSED:
                # Stale heartbeat check
                hb = token.last_heartbeat_at
                if hb is not None and (now - hb) > heartbeat_silence_s:
                    should_expire = True
            elif token.state in (
                BudgetTokenState.ACQUIRED,
                BudgetTokenState.TRANSFERRED,
            ):
                # No heartbeat + age exceeded
                if token.last_heartbeat_at is None and (
                    now - token.acquired_at
                ) > max_age_s:
                    should_expire = True

            if should_expire:
                token.state = BudgetTokenState.EXPIRED
                if token.category is HeavyTaskCategory.MODEL_LOAD:
                    self._active_model_load_count = max(
                        0, self._active_model_load_count - 1
                    )
                expired_count += 1

        return expired_count

    # -- Invariant check ----------------------------------------------------

    def _check_invariant(self) -> None:
        """If ``_active_model_load_count > 1``: set ``_frozen`` and log CRITICAL.

        Does NOT force-release tokens — the caller decides recovery strategy.
        """
        if self._active_model_load_count > 1:
            self._frozen = True
            _log.critical(
                "ECAPA budget invariant violated: "
                "_active_model_load_count=%d (>1). Bridge frozen.",
                self._active_model_load_count,
            )
