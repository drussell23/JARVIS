"""RoutingAuthorityFSM -- fail-closed routing authority state machine.

Disease 10 -- Startup Sequencing, Task 3.

Explicit state machine enforcing single-writer routing authority during
startup.  States: BOOT_POLICY_ACTIVE, HANDOFF_PENDING, HYBRID_ACTIVE,
HANDOFF_FAILED.  Transitions have guard checks evaluated in deterministic
order.  Transitions are journaled for restart recovery.

On restart recovery, if the journal's last entry indicates HANDOFF_PENDING
or HANDOFF_FAILED, the FSM rolls back to BOOT_POLICY_ACTIVE (safe default).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["AuthorityState", "TransitionResult", "RoutingAuthorityFSM"]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AuthorityState(str, Enum):
    """Possible states of the routing authority FSM."""

    BOOT_POLICY_ACTIVE = "BOOT_POLICY_ACTIVE"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    HYBRID_ACTIVE = "HYBRID_ACTIVE"
    HANDOFF_FAILED = "HANDOFF_FAILED"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """Immutable result of a state transition attempt."""

    success: bool
    from_state: str
    to_state: str
    failed_guard: Optional[str] = None
    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Authority holder mapping
# ---------------------------------------------------------------------------

_STATE_TO_AUTHORITY: Dict[AuthorityState, str] = {
    AuthorityState.BOOT_POLICY_ACTIVE: "boot_policy",
    AuthorityState.HANDOFF_PENDING: "handoff_controller",
    AuthorityState.HYBRID_ACTIVE: "hybrid_router",
    AuthorityState.HANDOFF_FAILED: "handoff_controller",
}

# ---------------------------------------------------------------------------
# Guard priority orders
# ---------------------------------------------------------------------------

_BEGIN_HANDOFF_GUARD_ORDER = (
    "core_ready_passed",
    "contracts_valid",
    "invariants_clean",
)

_COMPLETE_HANDOFF_GUARD_ORDER = (
    "contracts_valid",
    "invariants_clean",
    "hybrid_router_ready",
    "lease_or_local_ready",
    "readiness_contract_passed",
    "no_in_flight_requests",
)

# States that trigger recovery rollback to BOOT_POLICY_ACTIVE on restart
_UNSAFE_RECOVERY_STATES = frozenset({
    AuthorityState.HANDOFF_PENDING.value,
    AuthorityState.HANDOFF_FAILED.value,
})


# ---------------------------------------------------------------------------
# RoutingAuthorityFSM
# ---------------------------------------------------------------------------


class RoutingAuthorityFSM:
    """Fail-closed routing authority state machine.

    Enforces single-writer routing authority during startup via explicit
    state transitions with deterministic guard evaluation and journaling
    for restart recovery.

    Parameters
    ----------
    journal_path:
        Optional path to a JSONL file for transition journaling.
        If the file exists on construction, the last entry is inspected
        and the FSM recovers to BOOT_POLICY_ACTIVE if the last state
        was HANDOFF_PENDING or HANDOFF_FAILED.
    """

    def __init__(self, journal_path: Optional[str] = None) -> None:
        self._state: AuthorityState = AuthorityState.BOOT_POLICY_ACTIVE
        self._authority_holder: str = "boot_policy"
        self._log: List[Dict] = []
        self._journal_path: Optional[str] = journal_path

        # Recover from journal if it exists
        if journal_path is not None:
            self._recover_from_journal(journal_path)

    # -- Properties ----------------------------------------------------------

    @property
    def state(self) -> AuthorityState:
        """Current FSM state."""
        return self._state

    @property
    def authority_holder(self) -> str:
        """Current authority holder name."""
        return self._authority_holder

    @property
    def transition_log(self) -> List[Dict]:
        """Copy of the internal transition log (safe to mutate)."""
        return list(self._log)

    # -- Query ---------------------------------------------------------------

    def is_authority(self, holder: str) -> bool:
        """Return True if *holder* is the current authority holder."""
        return self._authority_holder == holder

    # -- Transitions ---------------------------------------------------------

    def begin_handoff(self, guards: Dict[str, bool]) -> TransitionResult:
        """Attempt BOOT_POLICY_ACTIVE -> HANDOFF_PENDING.

        Guards are evaluated in deterministic priority order:
        ``core_ready_passed``, ``contracts_valid``, ``invariants_clean``.

        Parameters
        ----------
        guards:
            Dict mapping guard names to their boolean outcomes.

        Returns
        -------
        TransitionResult
            Success if all guards pass and current state is correct.
        """
        current = self._state

        # Wrong-state check
        if current != AuthorityState.BOOT_POLICY_ACTIVE:
            logger.warning(
                "begin_handoff rejected: current state is %s, expected BOOT_POLICY_ACTIVE",
                current.value,
            )
            return TransitionResult(
                success=False,
                from_state=current.value,
                to_state=current.value,
                failed_guard=None,
            )

        # Evaluate guards in priority order
        for guard_name in _BEGIN_HANDOFF_GUARD_ORDER:
            if not guards.get(guard_name, False):
                logger.warning("begin_handoff guard failed: %s", guard_name)
                self._record_transition(
                    from_state=current.value,
                    to_state=current.value,
                    cause=f"guard_failed:{guard_name}",
                    failed_guard=guard_name,
                )
                return TransitionResult(
                    success=False,
                    from_state=current.value,
                    to_state=current.value,
                    failed_guard=guard_name,
                )

        # All guards passed -- transition
        new_state = AuthorityState.HANDOFF_PENDING
        self._state = new_state
        self._authority_holder = _STATE_TO_AUTHORITY[new_state]

        self._record_transition(
            from_state=current.value,
            to_state=new_state.value,
            cause="begin_handoff",
        )

        logger.info(
            "begin_handoff: %s -> %s (holder=%s)",
            current.value,
            new_state.value,
            self._authority_holder,
        )

        return TransitionResult(
            success=True,
            from_state=current.value,
            to_state=new_state.value,
        )

    def complete_handoff(self, guards: Dict[str, bool]) -> TransitionResult:
        """Attempt HANDOFF_PENDING -> HYBRID_ACTIVE.

        Guards are evaluated in deterministic priority order (cheap/static
        first, then dynamic, then drain):
        ``contracts_valid``, ``invariants_clean``, ``hybrid_router_ready``,
        ``lease_or_local_ready``, ``readiness_contract_passed``,
        ``no_in_flight_requests``.

        On guard failure, transitions to HANDOFF_FAILED instead of staying
        in HANDOFF_PENDING.

        Parameters
        ----------
        guards:
            Dict mapping guard names to their boolean outcomes.

        Returns
        -------
        TransitionResult
            Success if all guards pass.  On failure, FSM moves to
            HANDOFF_FAILED.
        """
        current = self._state

        # Wrong-state check
        if current != AuthorityState.HANDOFF_PENDING:
            logger.warning(
                "complete_handoff rejected: current state is %s, expected HANDOFF_PENDING",
                current.value,
            )
            return TransitionResult(
                success=False,
                from_state=current.value,
                to_state=current.value,
                failed_guard=None,
            )

        # Evaluate guards in priority order
        for guard_name in _COMPLETE_HANDOFF_GUARD_ORDER:
            if not guards.get(guard_name, False):
                logger.warning("complete_handoff guard failed: %s", guard_name)

                # Transition to HANDOFF_FAILED
                failed_state = AuthorityState.HANDOFF_FAILED
                self._state = failed_state
                self._authority_holder = _STATE_TO_AUTHORITY[failed_state]

                self._record_transition(
                    from_state=current.value,
                    to_state=failed_state.value,
                    cause=f"guard_failed:{guard_name}",
                    failed_guard=guard_name,
                )

                return TransitionResult(
                    success=False,
                    from_state=current.value,
                    to_state=failed_state.value,
                    failed_guard=guard_name,
                )

        # All guards passed -- transition to HYBRID_ACTIVE
        new_state = AuthorityState.HYBRID_ACTIVE
        self._state = new_state
        self._authority_holder = _STATE_TO_AUTHORITY[new_state]

        self._record_transition(
            from_state=current.value,
            to_state=new_state.value,
            cause="complete_handoff",
        )

        logger.info(
            "complete_handoff: %s -> %s (holder=%s)",
            current.value,
            new_state.value,
            self._authority_holder,
        )

        return TransitionResult(
            success=True,
            from_state=current.value,
            to_state=new_state.value,
        )

    def rollback(self, cause: str) -> TransitionResult:
        """Rollback to BOOT_POLICY_ACTIVE from any state.

        If already in BOOT_POLICY_ACTIVE, returns success as a no-op.

        Parameters
        ----------
        cause:
            Human-readable reason for the rollback.

        Returns
        -------
        TransitionResult
            Always succeeds.
        """
        current = self._state

        if current == AuthorityState.BOOT_POLICY_ACTIVE:
            logger.debug("rollback from BOOT_POLICY_ACTIVE is a no-op")
            return TransitionResult(
                success=True,
                from_state=current.value,
                to_state=current.value,
            )

        new_state = AuthorityState.BOOT_POLICY_ACTIVE
        self._state = new_state
        self._authority_holder = _STATE_TO_AUTHORITY[new_state]

        self._record_transition(
            from_state=current.value,
            to_state=new_state.value,
            cause=cause,
        )

        logger.info(
            "rollback: %s -> %s (cause=%s, holder=%s)",
            current.value,
            new_state.value,
            cause,
            self._authority_holder,
        )

        return TransitionResult(
            success=True,
            from_state=current.value,
            to_state=new_state.value,
        )

    # -- Internal helpers ----------------------------------------------------

    def _record_transition(
        self,
        from_state: str,
        to_state: str,
        cause: str = "",
        failed_guard: Optional[str] = None,
    ) -> None:
        """Record a transition in the in-memory log and journal."""
        entry: Dict = {
            "from_state": from_state,
            "to_state": to_state,
            "timestamp": time.monotonic(),
            "cause": cause,
            "failed_guard": failed_guard,
        }
        self._log.append(entry)
        self._journal_write(entry)

    def _journal_write(self, entry: Dict) -> None:
        """Append a JSON line to the journal file, if configured."""
        if self._journal_path is None:
            return
        try:
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.exception("Failed to write journal entry to %s", self._journal_path)

    def _recover_from_journal(self, journal_path: str) -> None:
        """Load journal and roll back to BOOT_POLICY_ACTIVE if last state is unsafe.

        Unsafe states for recovery: HANDOFF_PENDING, HANDOFF_FAILED.
        """
        try:
            with open(journal_path, "r", encoding="utf-8") as f:
                lines = f.read().strip().split("\n")
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Failed to read journal at %s", journal_path)
            return

        if not lines or lines == [""]:
            return

        try:
            last_entry = json.loads(lines[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning("Corrupt journal entry at %s, starting fresh", journal_path)
            return

        last_to_state = last_entry.get("to_state", "")

        if last_to_state in _UNSAFE_RECOVERY_STATES:
            logger.info(
                "Journal recovery: last state was %s, rolling back to BOOT_POLICY_ACTIVE",
                last_to_state,
            )
            # Stay in BOOT_POLICY_ACTIVE (already the default)
            self._state = AuthorityState.BOOT_POLICY_ACTIVE
            self._authority_holder = "boot_policy"
        elif last_to_state == AuthorityState.HYBRID_ACTIVE.value:
            # Restore to HYBRID_ACTIVE if that was the last committed state
            self._state = AuthorityState.HYBRID_ACTIVE
            self._authority_holder = _STATE_TO_AUTHORITY[AuthorityState.HYBRID_ACTIVE]
        # else: BOOT_POLICY_ACTIVE is already the default, nothing to do
