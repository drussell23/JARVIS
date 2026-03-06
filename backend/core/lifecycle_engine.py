"""
Lifecycle Engine (Disease 5+6 MVP)
===================================
Guarded state machine with transition table, epoch tracking,
and centralized state mutation.

KernelState is re-exported here for testability (the original
lives in unified_supervisor.py but is hard to import in tests).
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from backend.core.lifecycle_exceptions import (
    LifecycleFatalError,
    LifecyclePhase,
    LifecycleErrorCode,
    TransitionRejected,
)


# Re-export KernelState so tests and other modules can import it
# without pulling in the 98K-line unified_supervisor.py.
class KernelState(Enum):
    """States of the system kernel."""
    INITIALIZING = "initializing"
    PREFLIGHT = "preflight"
    STARTING_RESOURCES = "starting_resources"
    STARTING_BACKEND = "starting_backend"
    STARTING_INTELLIGENCE = "starting_intelligence"
    STARTING_TRINITY = "starting_trinity"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


class LifecycleEvent(str, Enum):
    """Typed events that drive state transitions."""
    PREFLIGHT_START = "preflight_start"
    BRINGUP_START = "bringup_start"
    BACKEND_START = "backend_start"
    INTEL_START = "intel_start"
    TRINITY_START = "trinity_start"
    READY = "ready"
    SHUTDOWN = "shutdown"
    STOPPED = "stopped"
    FATAL = "fatal"


@dataclass(frozen=True)
class TransitionRecord:
    """Stable schema for transition audit trail."""
    old_state: str
    event: str
    new_state: str
    epoch: int
    actor: str
    at_monotonic: float
    reason: str


# Transition table: (from_state, event) -> to_state
VALID_TRANSITIONS: Dict[Tuple[KernelState, LifecycleEvent], KernelState] = {
    # Forward startup sequence
    (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START):       KernelState.PREFLIGHT,
    (KernelState.PREFLIGHT, LifecycleEvent.BRINGUP_START):            KernelState.STARTING_RESOURCES,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.BACKEND_START):   KernelState.STARTING_BACKEND,
    (KernelState.STARTING_BACKEND, LifecycleEvent.INTEL_START):       KernelState.STARTING_INTELLIGENCE,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.TRINITY_START): KernelState.STARTING_TRINITY,
    (KernelState.STARTING_TRINITY, LifecycleEvent.READY):             KernelState.RUNNING,

    # Shutdown from any active state
    (KernelState.RUNNING, LifecycleEvent.SHUTDOWN):                   KernelState.SHUTTING_DOWN,
    (KernelState.PREFLIGHT, LifecycleEvent.SHUTDOWN):                 KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.SHUTDOWN):        KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_BACKEND, LifecycleEvent.SHUTDOWN):          KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.SHUTDOWN):     KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_TRINITY, LifecycleEvent.SHUTDOWN):          KernelState.SHUTTING_DOWN,

    # Idempotent duplicate shutdown
    (KernelState.SHUTTING_DOWN, LifecycleEvent.SHUTDOWN):             KernelState.SHUTTING_DOWN,

    # Completion
    (KernelState.SHUTTING_DOWN, LifecycleEvent.STOPPED):              KernelState.STOPPED,

    # Fatal from any non-terminal state
    (KernelState.INITIALIZING, LifecycleEvent.FATAL):                 KernelState.FAILED,
    (KernelState.PREFLIGHT, LifecycleEvent.FATAL):                    KernelState.FAILED,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.FATAL):           KernelState.FAILED,
    (KernelState.STARTING_BACKEND, LifecycleEvent.FATAL):             KernelState.FAILED,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.FATAL):        KernelState.FAILED,
    (KernelState.STARTING_TRINITY, LifecycleEvent.FATAL):             KernelState.FAILED,
    (KernelState.RUNNING, LifecycleEvent.FATAL):                      KernelState.FAILED,
    (KernelState.SHUTTING_DOWN, LifecycleEvent.FATAL):                KernelState.FAILED,
}

_logger = logging.getLogger(__name__)


class LifecycleEngine:
    """Single authority for all state transitions.

    Thread-safe for mutation via threading.Lock().
    Async callers use request_transition() which delegates.
    Signal-thread callers use loop.call_soon_threadsafe().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = KernelState.INITIALIZING
        self._epoch = 0
        self._history: deque = deque(maxlen=100)
        self._listeners: List[Callable] = []

    def transition(self, event: LifecycleEvent, *,
                   actor: str = "", reason: str = "") -> KernelState:
        """Attempt a guarded state transition. Thread-safe.

        Returns new state on success.
        Raises TransitionRejected for expected races (terminal state).
        Raises LifecycleFatalError for true invariant violations.
        """
        with self._lock:
            key = (self._state, event)
            if key not in VALID_TRANSITIONS:
                if self._state in (KernelState.STOPPED, KernelState.FAILED):
                    raise TransitionRejected(
                        f"Transition rejected: {self._state.value} is terminal",
                        error_code=LifecycleErrorCode.TRANSITION_INVALID,
                        state_at_raise=self._state.value,
                        phase=self._state_to_phase(),
                        epoch=self._epoch,
                    )
                raise LifecycleFatalError(
                    f"Invalid transition: {self._state.value} + {event.value}",
                    error_code=LifecycleErrorCode.TRANSITION_INVALID,
                    state_at_raise=self._state.value,
                    phase=self._state_to_phase(),
                    epoch=self._epoch,
                )
            old = self._state
            new = VALID_TRANSITIONS[key]
            is_noop = (old == new)
            self._state = new

            # Epoch increments on lifecycle session start
            if event == LifecycleEvent.PREFLIGHT_START:
                self._epoch += 1

            self._history.append(TransitionRecord(
                old_state=old.value, event=event.value,
                new_state=new.value, epoch=self._epoch,
                actor=actor, at_monotonic=time.monotonic(),
                reason=reason,
            ))

        if not is_noop:
            self._notify_listeners(old, event, new)
        return new

    async def request_transition(self, event: LifecycleEvent,
                                  actor: str = "", reason: str = "") -> KernelState:
        """Async-friendly transition wrapper."""
        return self.transition(event, actor=actor, reason=reason)

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def state(self) -> KernelState:
        with self._lock:
            return self._state

    @property
    def history(self) -> List[TransitionRecord]:
        with self._lock:
            return list(self._history)

    def subscribe(self, listener: Callable) -> None:
        """Subscribe to transition events."""
        self._listeners.append(listener)

    def _notify_listeners(self, old: KernelState, event: LifecycleEvent,
                          new: KernelState) -> None:
        """Notify listeners. Failures are isolated."""
        for listener in self._listeners:
            try:
                listener(old, event, new)
            except Exception as e:
                _logger.warning(
                    "[LifecycleEngine] Listener %s failed: %s",
                    getattr(listener, '__name__', repr(listener)), e,
                )

    def _state_to_phase(self) -> LifecyclePhase:
        _MAP = {
            KernelState.INITIALIZING: LifecyclePhase.PRECHECK,
            KernelState.PREFLIGHT: LifecyclePhase.PRECHECK,
            KernelState.STARTING_RESOURCES: LifecyclePhase.BRINGUP,
            KernelState.STARTING_BACKEND: LifecyclePhase.BRINGUP,
            KernelState.STARTING_INTELLIGENCE: LifecyclePhase.BRINGUP,
            KernelState.STARTING_TRINITY: LifecyclePhase.BRINGUP,
            KernelState.RUNNING: LifecyclePhase.RUNNING,
            KernelState.SHUTTING_DOWN: LifecyclePhase.STOPPING,
            KernelState.STOPPED: LifecyclePhase.STOPPED,
            KernelState.FAILED: LifecyclePhase.STOPPED,
        }
        return _MAP.get(self._state, LifecyclePhase.RUNNING)
