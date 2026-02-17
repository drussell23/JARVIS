"""
Finite State Machine v1.0 — Generic, reusable, async-safe FSM.

Generalized from AtomicStateMachine (backend/core/connection/state_machine.py)
to work with ANY Enum-based state type. Uses Compare-And-Swap (CAS) pattern
for concurrent safety.

Applicable to: VM lifecycle, model loading, startup phases, connection states,
authentication flows, etc.

Usage:
    from enum import Enum, auto
    from backend.core.fsm import FiniteStateMachine, TransitionRule

    class VMState(Enum):
        CREATING = auto()
        RUNNING = auto()
        STOPPING = auto()
        TERMINATED = auto()

    # Define allowed transitions
    rules = [
        TransitionRule(VMState.CREATING, VMState.RUNNING),
        TransitionRule(VMState.CREATING, VMState.TERMINATED),
        TransitionRule(VMState.RUNNING, VMState.STOPPING),
        TransitionRule(VMState.STOPPING, VMState.TERMINATED),
    ]

    fsm = FiniteStateMachine(VMState.CREATING, allowed_transitions=rules)

    # CAS transition
    success = await fsm.try_transition(VMState.CREATING, VMState.RUNNING, reason="Boot complete")
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
)

logger = logging.getLogger("jarvis.fsm")

S = TypeVar("S", bound=Enum)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransitionRule:
    """Defines an allowed state transition."""
    from_state: Enum
    to_state: Enum
    guard: Optional[Callable[[], bool]] = None  # Optional guard condition


@dataclass(frozen=True)
class TransitionRecord:
    """Immutable record of a state transition."""
    from_state: Enum
    to_state: Enum
    timestamp: float
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class TransitionError(Exception):
    """Raised when a state transition is invalid."""
    def __init__(self, from_state: Enum, to_state: Enum, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        msg = f"Invalid transition: {from_state.name} -> {to_state.name}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Finite State Machine
# ---------------------------------------------------------------------------

class FiniteStateMachine(Generic[S]):
    """
    Generic async-safe finite state machine with CAS transitions.

    Features:
        - Works with any Enum state type
        - Optional transition rules (whitelist of allowed transitions)
        - CAS pattern prevents thundering herd
        - Observer callbacks (called outside lock)
        - Bounded transition history
        - Both async and sync transition support
    """

    __slots__ = (
        "_state",
        "_lock",
        "_async_lock",
        "_loop_id",
        "_allowed_transitions",
        "_history",
        "_observers",
        "_max_history",
        "_transition_count",
        "_created_at",
    )

    def __init__(
        self,
        initial_state: S,
        *,
        allowed_transitions: Optional[List[TransitionRule]] = None,
        max_history: int = 100,
    ) -> None:
        self._state: S = initial_state
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None
        self._history: List[TransitionRecord] = []
        self._observers: List[Callable[[TransitionRecord], None]] = []
        self._max_history = max_history
        self._transition_count = 0
        self._created_at = time.monotonic()

        # Build allowed transitions set for O(1) lookup
        self._allowed_transitions: Optional[Set[Tuple[Enum, Enum]]] = None
        if allowed_transitions is not None:
            self._allowed_transitions = {
                (r.from_state, r.to_state) for r in allowed_transitions
            }

    # -- Async lock management -----------------------------------------------

    def _get_async_lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
            loop_id = id(loop)
        except RuntimeError:
            return asyncio.Lock()

        with self._lock:
            if self._loop_id != loop_id or self._async_lock is None:
                self._async_lock = asyncio.Lock()
                self._loop_id = loop_id
            return self._async_lock

    # -- State access --------------------------------------------------------

    @property
    def state(self) -> S:
        """Get current state (atomic read)."""
        with self._lock:
            return self._state

    @property
    def transition_count(self) -> int:
        with self._lock:
            return self._transition_count

    # -- Async CAS transition ------------------------------------------------

    async def try_transition(
        self,
        from_state: S,
        to_state: S,
        *,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Attempt atomic state transition using CAS pattern.

        Returns True if this caller won the transition, False otherwise.
        """
        async_lock = self._get_async_lock()
        record: Optional[TransitionRecord] = None

        async with async_lock:
            with self._lock:
                if self._state != from_state:
                    return False

                # Check allowed transitions
                if self._allowed_transitions is not None:
                    if (from_state, to_state) not in self._allowed_transitions:
                        logger.warning(
                            f"[FSM] Blocked transition: {from_state.name} -> {to_state.name}"
                        )
                        return False

                self._state = to_state
                self._transition_count += 1

                record = TransitionRecord(
                    from_state=from_state,
                    to_state=to_state,
                    timestamp=time.time(),
                    reason=reason,
                    metadata=metadata or {},
                )
                self._history.append(record)
                if len(self._history) > self._max_history:
                    self._history = self._history[-self._max_history:]

        # Notify observers outside lock
        if record is not None:
            self._notify_observers(record)

        logger.debug(f"[FSM] {from_state.name} -> {to_state.name}: {reason}")
        return True

    # -- Sync CAS transition -------------------------------------------------

    def try_transition_sync(
        self,
        from_state: S,
        to_state: S,
        *,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Synchronous CAS transition."""
        record: Optional[TransitionRecord] = None

        with self._lock:
            if self._state != from_state:
                return False

            if self._allowed_transitions is not None:
                if (from_state, to_state) not in self._allowed_transitions:
                    return False

            self._state = to_state
            self._transition_count += 1

            record = TransitionRecord(
                from_state=from_state,
                to_state=to_state,
                timestamp=time.time(),
                reason=reason,
                metadata=metadata or {},
            )
            self._history.append(record)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        if record is not None:
            self._notify_observers(record)
        return True

    # -- Force transition (bypasses CAS) -------------------------------------

    async def force_transition(
        self,
        to_state: S,
        *,
        reason: str = "",
    ) -> S:
        """
        Force transition regardless of current state.
        Returns the previous state. Use sparingly (error recovery, resets).
        """
        async_lock = self._get_async_lock()
        async with async_lock:
            with self._lock:
                old = self._state
                self._state = to_state
                self._transition_count += 1
                record = TransitionRecord(
                    from_state=old,
                    to_state=to_state,
                    timestamp=time.time(),
                    reason=f"FORCED: {reason}",
                )
                self._history.append(record)

        self._notify_observers(record)
        logger.warning(f"[FSM] FORCED: {old.name} -> {to_state.name}: {reason}")
        return old

    # -- Observers -----------------------------------------------------------

    def add_observer(self, callback: Callable[[TransitionRecord], None]) -> None:
        self._observers.append(callback)

    def remove_observer(self, callback: Callable[[TransitionRecord], None]) -> bool:
        try:
            self._observers.remove(callback)
            return True
        except ValueError:
            return False

    def _notify_observers(self, record: TransitionRecord) -> None:
        for observer in self._observers:
            try:
                observer(record)
            except Exception as exc:
                logger.warning(f"[FSM] Observer error: {exc}")

    # -- Query ---------------------------------------------------------------

    def is_in(self, *states: S) -> bool:
        """Check if current state is one of the given states."""
        with self._lock:
            return self._state in states

    def get_history(self, limit: int = 10) -> List[TransitionRecord]:
        with self._lock:
            return list(self._history[-limit:])

    def get_allowed_from(self, state: S) -> List[S]:
        """Get all states reachable from the given state."""
        if self._allowed_transitions is None:
            return []
        return [to for frm, to in self._allowed_transitions if frm == state]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "current_state": self._state.name,
                "transition_count": self._transition_count,
                "history_size": len(self._history),
                "uptime_s": round(time.monotonic() - self._created_at, 1),
                "observers": len(self._observers),
                "constrained": self._allowed_transitions is not None,
            }

    def __repr__(self) -> str:
        return f"FiniteStateMachine(state={self.state.name}, transitions={self.transition_count})"
