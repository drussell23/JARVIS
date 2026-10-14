"""
v77.1: Evolution State Machine - Gap #57
=========================================

Persistent state machine for evolution lifecycle.

Problem:
    - Crash during evolution loses all progress
    - No way to resume from partial completion
    - State is only in memory

Solution:
    - Finite state machine with persistence
    - Checkpoints at each state transition
    - Automatic recovery after crash
    - Transition guards and validations

States:
    PENDING → ANALYZING → PLANNING → EXECUTING → VALIDATING → COMMITTED

Features:
    - Atomic state transitions
    - Checkpoint recovery
    - Transition validation
    - State history
    - Timeout detection

Author: JARVIS v77.1
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, TypeVar

logger = logging.getLogger(__name__)


class EvolutionState(Enum):
    """
    States in the evolution lifecycle.

    Transitions:
        PENDING → ANALYZING → PLANNING → EXECUTING → VALIDATING → COMMITTED
                                   ↓                      ↓
                              ROLLING_BACK ← ← ← ← ← FAILED
                                   ↓
                              ROLLED_BACK
    """
    # Initial states
    PENDING = "pending"
    QUEUED = "queued"

    # Analysis states
    ANALYZING = "analyzing"
    ANALYSIS_COMPLETE = "analysis_complete"

    # Planning states
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    AWAITING_APPROVAL = "awaiting_approval"

    # Execution states
    EXECUTING = "executing"
    EXECUTION_COMPLETE = "execution_complete"

    # Validation states
    VALIDATING = "validating"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"

    # Commit states
    COMMITTING = "committing"
    COMMITTED = "committed"

    # Failure states
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"

    # Terminal states
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass
class StateTransition:
    """Record of a state transition."""
    from_state: EvolutionState
    to_state: EvolutionState
    timestamp: float = field(default_factory=time.time)
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass
class Checkpoint:
    """
    A checkpoint of evolution state.

    Can be used to restore progress after crash.
    """
    evolution_id: str
    state: EvolutionState
    timestamp: float = field(default_factory=time.time)
    context: Dict[str, Any] = field(default_factory=dict)
    transitions: List[StateTransition] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evolution_id": self.evolution_id,
            "state": self.state.value,
            "timestamp": self.timestamp,
            "context": self.context,
            "transitions": [t.to_dict() for t in self.transitions],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Checkpoint":
        transitions = [
            StateTransition(
                from_state=EvolutionState(t["from_state"]),
                to_state=EvolutionState(t["to_state"]),
                timestamp=t["timestamp"],
                reason=t.get("reason", ""),
                metadata=t.get("metadata", {}),
            )
            for t in data.get("transitions", [])
        ]

        return cls(
            evolution_id=data["evolution_id"],
            state=EvolutionState(data["state"]),
            timestamp=data["timestamp"],
            context=data.get("context", {}),
            transitions=transitions,
            metadata=data.get("metadata", {}),
        )


class TransitionGuard:
    """
    Guard for state transitions.

    Validates that a transition is allowed before executing.
    """

    # Valid transitions
    VALID_TRANSITIONS: Dict[EvolutionState, Set[EvolutionState]] = {
        EvolutionState.PENDING: {EvolutionState.QUEUED, EvolutionState.ANALYZING, EvolutionState.ABORTED},
        EvolutionState.QUEUED: {EvolutionState.ANALYZING, EvolutionState.ABORTED},

        EvolutionState.ANALYZING: {EvolutionState.ANALYSIS_COMPLETE, EvolutionState.FAILED},
        EvolutionState.ANALYSIS_COMPLETE: {EvolutionState.PLANNING, EvolutionState.ABORTED},

        EvolutionState.PLANNING: {EvolutionState.PLAN_READY, EvolutionState.FAILED},
        EvolutionState.PLAN_READY: {EvolutionState.AWAITING_APPROVAL, EvolutionState.EXECUTING},
        EvolutionState.AWAITING_APPROVAL: {EvolutionState.EXECUTING, EvolutionState.ABORTED},

        EvolutionState.EXECUTING: {EvolutionState.EXECUTION_COMPLETE, EvolutionState.FAILED},
        EvolutionState.EXECUTION_COMPLETE: {EvolutionState.VALIDATING},

        EvolutionState.VALIDATING: {EvolutionState.VALIDATION_PASSED, EvolutionState.VALIDATION_FAILED},
        EvolutionState.VALIDATION_PASSED: {EvolutionState.COMMITTING},
        EvolutionState.VALIDATION_FAILED: {EvolutionState.ROLLING_BACK, EvolutionState.FAILED},

        EvolutionState.COMMITTING: {EvolutionState.COMMITTED, EvolutionState.FAILED},
        EvolutionState.COMMITTED: {EvolutionState.COMPLETED},

        EvolutionState.FAILED: {EvolutionState.ROLLING_BACK, EvolutionState.ABORTED},
        EvolutionState.ROLLING_BACK: {EvolutionState.ROLLED_BACK, EvolutionState.FAILED},
        EvolutionState.ROLLED_BACK: {EvolutionState.COMPLETED, EvolutionState.ABORTED},

        EvolutionState.COMPLETED: set(),  # Terminal state
        EvolutionState.ABORTED: set(),  # Terminal state
    }

    @classmethod
    def is_valid_transition(
        cls,
        from_state: EvolutionState,
        to_state: EvolutionState,
    ) -> bool:
        """Check if a transition is valid."""
        valid_targets = cls.VALID_TRANSITIONS.get(from_state, set())
        return to_state in valid_targets

    @classmethod
    def get_valid_transitions(cls, state: EvolutionState) -> Set[EvolutionState]:
        """Get valid transitions from a state."""
        return cls.VALID_TRANSITIONS.get(state, set()).copy()

    @classmethod
    def is_terminal(cls, state: EvolutionState) -> bool:
        """Check if state is terminal."""
        return state in {EvolutionState.COMPLETED, EvolutionState.ABORTED}

    @classmethod
    def is_recoverable(cls, state: EvolutionState) -> bool:
        """Check if state can be recovered from after crash."""
        # These states can be resumed
        return state in {
            EvolutionState.PENDING,
            EvolutionState.QUEUED,
            EvolutionState.ANALYZING,
            EvolutionState.PLANNING,
            EvolutionState.AWAITING_APPROVAL,
            EvolutionState.ROLLING_BACK,
        }


class EvolutionStateMachine:
    """
    State machine for managing evolution lifecycle.

    Features:
    - Atomic state transitions with validation
    - Persistent checkpoints for crash recovery
    - State history tracking
    - Transition callbacks

    Usage:
        sm = EvolutionStateMachine("ev123")
        await sm.start()

        # Transition through states
        await sm.transition_to(EvolutionState.ANALYZING)
        await sm.transition_to(EvolutionState.ANALYSIS_COMPLETE, context={"files": [...]})

        # Save checkpoint
        await sm.save_checkpoint()

        # Later, recover from checkpoint
        sm2 = EvolutionStateMachine("ev123")
        await sm2.restore()
    """

    def __init__(
        self,
        evolution_id: str,
        checkpoint_dir: Optional[Path] = None,
        auto_checkpoint: bool = True,
    ):
        self.evolution_id = evolution_id
        self.checkpoint_dir = checkpoint_dir or Path.home() / ".jarvis" / "evolution_state"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.auto_checkpoint = auto_checkpoint

        self._state = EvolutionState.PENDING
        self._context: Dict[str, Any] = {}
        self._transitions: List[StateTransition] = []
        self._metadata: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._started_at: float = 0.0

        # Callbacks
        self._on_transition: List[Callable[[StateTransition], Coroutine]] = []
        self._on_state: Dict[EvolutionState, List[Callable[[], Coroutine]]] = {}

    @property
    def state(self) -> EvolutionState:
        """Current state."""
        return self._state

    @property
    def context(self) -> Dict[str, Any]:
        """Current context data."""
        return self._context.copy()

    @property
    def is_terminal(self) -> bool:
        """Check if in terminal state."""
        return TransitionGuard.is_terminal(self._state)

    @property
    def duration_ms(self) -> float:
        """Time since evolution started."""
        if not self._started_at:
            return 0.0
        return (time.time() - self._started_at) * 1000

    async def start(self) -> None:
        """Start the state machine."""
        self._started_at = time.time()
        self._metadata["started_at"] = self._started_at
        logger.info(f"[StateMachine:{self.evolution_id}] Started in state {self._state.value}")

    async def transition_to(
        self,
        new_state: EvolutionState,
        reason: str = "",
        context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Transition to a new state.

        Args:
            new_state: The target state
            reason: Reason for transition
            context: Context data to merge
            metadata: Metadata to record

        Returns:
            True if transition succeeded

        Raises:
            ValueError: If transition is invalid
        """
        async with self._lock:
            # Validate transition
            if not TransitionGuard.is_valid_transition(self._state, new_state):
                valid = TransitionGuard.get_valid_transitions(self._state)
                raise ValueError(
                    f"Invalid transition: {self._state.value} → {new_state.value}. "
                    f"Valid transitions: {[s.value for s in valid]}"
                )

            old_state = self._state

            # Create transition record
            transition = StateTransition(
                from_state=old_state,
                to_state=new_state,
                reason=reason,
                metadata=metadata or {},
            )

            # Update state
            self._state = new_state
            self._transitions.append(transition)

            # Merge context
            if context:
                self._context.update(context)

            logger.info(
                f"[StateMachine:{self.evolution_id}] "
                f"{old_state.value} → {new_state.value}"
                f"{f' ({reason})' if reason else ''}"
            )

            # Auto checkpoint
            if self.auto_checkpoint:
                await self.save_checkpoint()

            # Notify callbacks
            await self._notify_transition(transition)
            await self._notify_state(new_state)

            return True

    async def save_checkpoint(self) -> str:
        """
        Save current state to checkpoint.

        Returns:
            Path to checkpoint file
        """
        checkpoint = Checkpoint(
            evolution_id=self.evolution_id,
            state=self._state,
            context=self._context,
            transitions=self._transitions,
            metadata=self._metadata,
        )

        # Atomic write
        checkpoint_file = self.checkpoint_dir / f"{self.evolution_id}.json"
        tmp_file = checkpoint_file.with_suffix(".tmp")

        tmp_file.write_text(json.dumps(checkpoint.to_dict(), indent=2, default=str))
        tmp_file.replace(checkpoint_file)

        logger.debug(f"[StateMachine:{self.evolution_id}] Checkpoint saved")
        return str(checkpoint_file)

    async def restore(self) -> bool:
        """
        Restore state from checkpoint.

        Returns:
            True if restored, False if no checkpoint
        """
        checkpoint_file = self.checkpoint_dir / f"{self.evolution_id}.json"
        if not checkpoint_file.exists():
            return False

        try:
            data = json.loads(checkpoint_file.read_text())
            checkpoint = Checkpoint.from_dict(data)

            self._state = checkpoint.state
            self._context = checkpoint.context
            self._transitions = checkpoint.transitions
            self._metadata = checkpoint.metadata
            self._started_at = checkpoint.metadata.get("started_at", time.time())

            logger.info(
                f"[StateMachine:{self.evolution_id}] "
                f"Restored from checkpoint (state={self._state.value})"
            )
            return True

        except Exception as e:
            logger.error(f"[StateMachine:{self.evolution_id}] Restore failed: {e}")
            return False

    async def delete_checkpoint(self) -> None:
        """Delete checkpoint file."""
        checkpoint_file = self.checkpoint_dir / f"{self.evolution_id}.json"
        if checkpoint_file.exists():
            checkpoint_file.unlink()

    def on_transition(self, callback: Callable[[StateTransition], Coroutine]) -> None:
        """Register transition callback."""
        self._on_transition.append(callback)

    def on_state(
        self,
        state: EvolutionState,
        callback: Callable[[], Coroutine],
    ) -> None:
        """Register callback for specific state."""
        if state not in self._on_state:
            self._on_state[state] = []
        self._on_state[state].append(callback)

    async def _notify_transition(self, transition: StateTransition) -> None:
        """Notify transition callbacks."""
        for callback in self._on_transition:
            try:
                await callback(transition)
            except Exception as e:
                logger.error(f"[StateMachine] Transition callback error: {e}")

    async def _notify_state(self, state: EvolutionState) -> None:
        """Notify state callbacks."""
        callbacks = self._on_state.get(state, [])
        for callback in callbacks:
            try:
                await callback()
            except Exception as e:
                logger.error(f"[StateMachine] State callback error: {e}")

    def get_history(self) -> List[Dict[str, Any]]:
        """Get transition history."""
        return [t.to_dict() for t in self._transitions]

    def get_status(self) -> Dict[str, Any]:
        """Get current status."""
        return {
            "evolution_id": self.evolution_id,
            "state": self._state.value,
            "is_terminal": self.is_terminal,
            "duration_ms": self.duration_ms,
            "transition_count": len(self._transitions),
            "context_keys": list(self._context.keys()),
        }

    async def wait_for_state(
        self,
        target_states: Set[EvolutionState],
        timeout: Optional[float] = None,
    ) -> EvolutionState:
        """
        Wait until the machine reaches one of the target states.

        Args:
            target_states: States to wait for
            timeout: Maximum wait time in seconds

        Returns:
            The state reached

        Raises:
            asyncio.TimeoutError: If timeout exceeded
        """
        event = asyncio.Event()
        reached_state: Optional[EvolutionState] = None

        async def on_transition(t: StateTransition):
            nonlocal reached_state
            if t.to_state in target_states:
                reached_state = t.to_state
                event.set()

        self.on_transition(on_transition)

        # Check if already in target state
        if self._state in target_states:
            return self._state

        if timeout:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        else:
            await event.wait()

        return reached_state or self._state


class StateMachineManager:
    """
    Manager for multiple state machines.

    Handles:
    - Creating state machines
    - Tracking active evolutions
    - Recovering crashed evolutions
    - Cleanup of old checkpoints
    """

    def __init__(
        self,
        checkpoint_dir: Optional[Path] = None,
        max_checkpoints: int = 100,
    ):
        self.checkpoint_dir = checkpoint_dir or Path.home() / ".jarvis" / "evolution_state"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.max_checkpoints = max_checkpoints

        self._machines: Dict[str, EvolutionStateMachine] = {}

    async def create(self, evolution_id: Optional[str] = None) -> EvolutionStateMachine:
        """Create a new state machine."""
        evolution_id = evolution_id or str(uuid.uuid4())

        sm = EvolutionStateMachine(
            evolution_id=evolution_id,
            checkpoint_dir=self.checkpoint_dir,
        )
        await sm.start()

        self._machines[evolution_id] = sm
        return sm

    async def get(self, evolution_id: str) -> Optional[EvolutionStateMachine]:
        """Get or restore a state machine."""
        if evolution_id in self._machines:
            return self._machines[evolution_id]

        # Try to restore from checkpoint
        sm = EvolutionStateMachine(
            evolution_id=evolution_id,
            checkpoint_dir=self.checkpoint_dir,
        )

        if await sm.restore():
            self._machines[evolution_id] = sm
            return sm

        return None

    async def recover_all(self) -> List[str]:
        """Recover all crashed evolutions."""
        recovered = []

        for checkpoint_file in self.checkpoint_dir.glob("*.json"):
            try:
                evolution_id = checkpoint_file.stem
                if evolution_id in self._machines:
                    continue

                sm = EvolutionStateMachine(
                    evolution_id=evolution_id,
                    checkpoint_dir=self.checkpoint_dir,
                )

                if await sm.restore():
                    if TransitionGuard.is_recoverable(sm.state):
                        self._machines[evolution_id] = sm
                        recovered.append(evolution_id)
                        logger.info(f"[StateMachineManager] Recovered: {evolution_id}")

            except Exception as e:
                logger.warning(f"[StateMachineManager] Recovery failed for {checkpoint_file}: {e}")

        return recovered

    async def cleanup_old_checkpoints(self) -> int:
        """Remove old checkpoints beyond max_checkpoints."""
        checkpoints = sorted(
            self.checkpoint_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        removed = 0
        for checkpoint in checkpoints[self.max_checkpoints:]:
            try:
                checkpoint.unlink()
                removed += 1
            except Exception:
                pass

        return removed

    def get_active_evolutions(self) -> List[str]:
        """Get IDs of active evolutions."""
        return [
            eid for eid, sm in self._machines.items()
            if not sm.is_terminal
        ]

    def get_statistics(self) -> Dict[str, Any]:
        """Get manager statistics."""
        states = {}
        for sm in self._machines.values():
            state = sm.state.value
            states[state] = states.get(state, 0) + 1

        return {
            "total_machines": len(self._machines),
            "active": len(self.get_active_evolutions()),
            "states": states,
            "checkpoints_on_disk": len(list(self.checkpoint_dir.glob("*.json"))),
        }


# Global instance
_manager: Optional[StateMachineManager] = None


def get_state_machine_manager() -> StateMachineManager:
    """Get or create global state machine manager."""
    global _manager
    if _manager is None:
        _manager = StateMachineManager()
    return _manager
