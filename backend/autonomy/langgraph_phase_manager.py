"""
LangGraph Phase Manager
=======================

Provides phase tracking for autonomous task execution using a state machine approach
inspired by LangGraph patterns. Enables intelligent phase transitions, confidence-based
routing, memory checkpoints, and learning consolidation.

v1.0: Initial implementation with phase state machine and checkpoint management.

Phases:
    ANALYZING    - Understanding the task, gathering context
    PLANNING     - Creating execution strategy
    EXECUTING    - Performing the task actions
    REFLECTING   - Evaluating results and outcomes
    LEARNING     - Consolidating insights for future tasks

Author: JARVIS AI System
"""

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class PhaseManagerConfig:
    """Configuration for the LangGraph Phase Manager."""

    # Phase transition settings
    min_analysis_confidence: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIN_ANALYSIS_CONFIDENCE", "0.7"))
    )
    min_planning_confidence: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIN_PLANNING_CONFIDENCE", "0.75"))
    )
    min_execution_confidence: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MIN_EXECUTION_CONFIDENCE", "0.8"))
    )

    # Timeout settings (seconds)
    analysis_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_ANALYSIS_TIMEOUT", "30"))
    )
    planning_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PLANNING_TIMEOUT", "60"))
    )
    execution_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_EXECUTION_TIMEOUT", "300"))
    )
    reflection_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_REFLECTION_TIMEOUT", "30"))
    )

    # Checkpoint settings
    checkpoint_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PHASE_CHECKPOINTS", "true").lower() == "true"
    )
    max_checkpoints_per_task: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MAX_CHECKPOINTS", "10"))
    )

    # Learning settings
    learning_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PHASE_LEARNING", "true").lower() == "true"
    )
    learning_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_LEARNING_THRESHOLD", "0.85"))
    )

    # Retry settings
    max_phase_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MAX_PHASE_RETRIES", "3"))
    )
    retry_backoff_base: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_RETRY_BACKOFF_BASE", "1.5"))
    )


# =============================================================================
# Phase Definitions
# =============================================================================


class Phase(Enum):
    """Execution phases for autonomous task processing."""

    IDLE = auto()
    ANALYZING = auto()
    PLANNING = auto()
    EXECUTING = auto()
    REFLECTING = auto()
    LEARNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    PAUSED = auto()


class PhaseTransition(Enum):
    """Valid phase transitions."""

    START = "start"
    ANALYZE_COMPLETE = "analyze_complete"
    PLAN_COMPLETE = "plan_complete"
    EXECUTE_COMPLETE = "execute_complete"
    REFLECT_COMPLETE = "reflect_complete"
    LEARN_COMPLETE = "learn_complete"
    PAUSE = "pause"
    RESUME = "resume"
    FAIL = "fail"
    RETRY = "retry"
    RESET = "reset"


# Valid state transitions
VALID_TRANSITIONS: Dict[Phase, Dict[PhaseTransition, Phase]] = {
    Phase.IDLE: {
        PhaseTransition.START: Phase.ANALYZING,
    },
    Phase.ANALYZING: {
        PhaseTransition.ANALYZE_COMPLETE: Phase.PLANNING,
        PhaseTransition.PAUSE: Phase.PAUSED,
        PhaseTransition.FAIL: Phase.FAILED,
        PhaseTransition.RETRY: Phase.ANALYZING,
    },
    Phase.PLANNING: {
        PhaseTransition.PLAN_COMPLETE: Phase.EXECUTING,
        PhaseTransition.PAUSE: Phase.PAUSED,
        PhaseTransition.FAIL: Phase.FAILED,
        PhaseTransition.RETRY: Phase.PLANNING,
    },
    Phase.EXECUTING: {
        PhaseTransition.EXECUTE_COMPLETE: Phase.REFLECTING,
        PhaseTransition.PAUSE: Phase.PAUSED,
        PhaseTransition.FAIL: Phase.FAILED,
        PhaseTransition.RETRY: Phase.EXECUTING,
    },
    Phase.REFLECTING: {
        PhaseTransition.REFLECT_COMPLETE: Phase.LEARNING,
        PhaseTransition.FAIL: Phase.FAILED,
    },
    Phase.LEARNING: {
        PhaseTransition.LEARN_COMPLETE: Phase.COMPLETED,
        PhaseTransition.FAIL: Phase.FAILED,
    },
    Phase.PAUSED: {
        PhaseTransition.RESUME: Phase.ANALYZING,  # Returns to last active phase
        PhaseTransition.FAIL: Phase.FAILED,
        PhaseTransition.RESET: Phase.IDLE,
    },
    Phase.COMPLETED: {
        PhaseTransition.RESET: Phase.IDLE,
    },
    Phase.FAILED: {
        PhaseTransition.RESET: Phase.IDLE,
        PhaseTransition.RETRY: Phase.ANALYZING,
    },
}


# =============================================================================
# State and Checkpoint Data Structures
# =============================================================================


@dataclass
class PhaseCheckpoint:
    """Represents a checkpoint at a specific phase."""

    checkpoint_id: str
    task_id: str
    phase: Phase
    timestamp: float
    confidence: float
    context: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseState:
    """Current state of phase execution."""

    task_id: str
    current_phase: Phase
    previous_phase: Optional[Phase]
    phase_start_time: float
    phase_confidence: float
    retry_count: int
    total_transitions: int
    checkpoints: List[PhaseCheckpoint]
    context: Dict[str, Any]
    errors: List[Dict[str, Any]]
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert state to dictionary."""
        return {
            "task_id": self.task_id,
            "current_phase": self.current_phase.name,
            "previous_phase": self.previous_phase.name if self.previous_phase else None,
            "phase_start_time": self.phase_start_time,
            "phase_confidence": self.phase_confidence,
            "retry_count": self.retry_count,
            "total_transitions": self.total_transitions,
            "checkpoint_count": len(self.checkpoints),
            "error_count": len(self.errors),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PhaseResult:
    """Result of a phase execution."""

    phase: Phase
    success: bool
    confidence: float
    output: Any
    duration: float
    error: Optional[str] = None
    next_phase: Optional[Phase] = None
    should_learn: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Phase Callbacks
# =============================================================================


@dataclass
class PhaseCallbacks:
    """Callbacks for phase lifecycle events."""

    on_phase_start: Optional[Callable[[str, Phase, Dict[str, Any]], None]] = None
    on_phase_complete: Optional[Callable[[str, Phase, PhaseResult], None]] = None
    on_phase_error: Optional[Callable[[str, Phase, Exception], None]] = None
    on_checkpoint_created: Optional[Callable[[PhaseCheckpoint], None]] = None
    on_learning_trigger: Optional[Callable[[str, Dict[str, Any]], None]] = None
    on_transition: Optional[Callable[[str, Phase, Phase, PhaseTransition], None]] = None


# =============================================================================
# LangGraph Phase Manager
# =============================================================================


class LangGraphPhaseManager:
    """
    Manages phase-based execution for autonomous tasks.

    Provides:
    - State machine for phase transitions
    - Confidence-based routing between phases
    - Memory checkpoints for recovery
    - Learning consolidation triggers
    - Phase timeout management
    """

    def __init__(
        self,
        config: Optional[PhaseManagerConfig] = None,
        callbacks: Optional[PhaseCallbacks] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the phase manager."""
        self.config = config or PhaseManagerConfig()
        self.callbacks = callbacks or PhaseCallbacks()
        self.logger = logger or logging.getLogger(__name__)

        # Active task states
        self._states: Dict[str, PhaseState] = {}
        self._phase_handlers: Dict[Phase, Callable] = {}

        # Statistics
        self._stats = {
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "total_transitions": 0,
            "checkpoints_created": 0,
            "learning_triggers": 0,
        }

        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the phase manager."""
        if self._initialized:
            return True

        try:
            self.logger.info("[PhaseManager] Initializing LangGraph Phase Manager...")
            self._initialized = True
            self.logger.info("[PhaseManager] ✓ Phase Manager initialized")
            return True

        except Exception as e:
            self.logger.error(f"[PhaseManager] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the phase manager."""
        if not self._initialized:
            return

        self.logger.info("[PhaseManager] Shutting down...")

        # Save any pending checkpoints
        for task_id, state in self._states.items():
            if state.current_phase not in (Phase.IDLE, Phase.COMPLETED, Phase.FAILED):
                await self.create_checkpoint(task_id, "shutdown")

        self._states.clear()
        self._initialized = False
        self.logger.info("[PhaseManager] ✓ Shutdown complete")

    # =========================================================================
    # Task Lifecycle
    # =========================================================================

    async def start_task(
        self,
        task_id: str,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> PhaseState:
        """Start a new task execution."""
        async with self._lock:
            if task_id in self._states:
                raise ValueError(f"Task {task_id} already exists")

            now = time.time()
            state = PhaseState(
                task_id=task_id,
                current_phase=Phase.IDLE,
                previous_phase=None,
                phase_start_time=now,
                phase_confidence=1.0,
                retry_count=0,
                total_transitions=0,
                checkpoints=[],
                context=context or {},
                errors=[],
                created_at=now,
                updated_at=now,
            )
            state.context["goal"] = goal

            self._states[task_id] = state
            self._stats["total_tasks"] += 1

            self.logger.info(f"[PhaseManager] Started task {task_id}: {goal[:50]}...")

            return state

    async def get_state(self, task_id: str) -> Optional[PhaseState]:
        """Get current state for a task."""
        return self._states.get(task_id)

    async def transition(
        self,
        task_id: str,
        transition: PhaseTransition,
        confidence: float = 1.0,
        context_update: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Phase]:
        """
        Attempt a phase transition.

        Returns:
            Tuple of (success, new_phase)
        """
        async with self._lock:
            state = self._states.get(task_id)
            if not state:
                self.logger.warning(f"[PhaseManager] Task {task_id} not found")
                return False, Phase.IDLE

            current = state.current_phase
            valid_transitions = VALID_TRANSITIONS.get(current, {})

            if transition not in valid_transitions:
                self.logger.warning(
                    f"[PhaseManager] Invalid transition {transition.value} from {current.name}"
                )
                return False, current

            # Handle RESUME specially - return to previous phase
            if transition == PhaseTransition.RESUME and state.previous_phase:
                new_phase = state.previous_phase
            else:
                new_phase = valid_transitions[transition]

            # Check confidence threshold for progression
            if transition in (
                PhaseTransition.ANALYZE_COMPLETE,
                PhaseTransition.PLAN_COMPLETE,
                PhaseTransition.EXECUTE_COMPLETE,
            ):
                min_confidence = self._get_min_confidence(current)
                if confidence < min_confidence:
                    self.logger.warning(
                        f"[PhaseManager] Confidence {confidence:.2f} below threshold "
                        f"{min_confidence:.2f} for {current.name}"
                    )
                    # Increment retry count
                    state.retry_count += 1
                    if state.retry_count >= self.config.max_phase_retries:
                        return await self.transition(task_id, PhaseTransition.FAIL)
                    return False, current

            # Perform transition
            state.previous_phase = current
            state.current_phase = new_phase
            state.phase_start_time = time.time()
            state.phase_confidence = confidence
            state.total_transitions += 1
            state.updated_at = time.time()

            if context_update:
                state.context.update(context_update)

            # Reset retry count on successful progression
            if transition not in (PhaseTransition.RETRY, PhaseTransition.FAIL):
                state.retry_count = 0

            # Update stats
            self._stats["total_transitions"] += 1
            if new_phase == Phase.COMPLETED:
                self._stats["completed_tasks"] += 1
            elif new_phase == Phase.FAILED:
                self._stats["failed_tasks"] += 1

            # Create checkpoint on phase transition
            if self.config.checkpoint_enabled:
                await self.create_checkpoint(task_id, f"transition_{transition.value}")

            # Invoke callback
            if self.callbacks.on_transition:
                try:
                    self.callbacks.on_transition(task_id, current, new_phase, transition)
                except Exception as e:
                    self.logger.error(f"[PhaseManager] Transition callback error: {e}")

            self.logger.info(
                f"[PhaseManager] Task {task_id}: {current.name} -> {new_phase.name} "
                f"(confidence: {confidence:.2f})"
            )

            return True, new_phase

    def _get_min_confidence(self, phase: Phase) -> float:
        """Get minimum confidence threshold for a phase."""
        thresholds = {
            Phase.ANALYZING: self.config.min_analysis_confidence,
            Phase.PLANNING: self.config.min_planning_confidence,
            Phase.EXECUTING: self.config.min_execution_confidence,
        }
        return thresholds.get(phase, 0.5)

    # =========================================================================
    # Phase Execution
    # =========================================================================

    async def execute_phase(
        self,
        task_id: str,
        phase_handler: Callable,
        phase_input: Any = None,
    ) -> PhaseResult:
        """
        Execute the current phase with the provided handler.

        Args:
            task_id: The task identifier
            phase_handler: Async function to execute for this phase
            phase_input: Input to pass to the handler

        Returns:
            PhaseResult with execution outcome
        """
        state = self._states.get(task_id)
        if not state:
            return PhaseResult(
                phase=Phase.IDLE,
                success=False,
                confidence=0.0,
                output=None,
                duration=0.0,
                error="Task not found",
            )

        phase = state.current_phase
        start_time = time.time()

        # Invoke phase start callback
        if self.callbacks.on_phase_start:
            try:
                self.callbacks.on_phase_start(task_id, phase, state.context)
            except Exception as e:
                self.logger.error(f"[PhaseManager] Phase start callback error: {e}")

        try:
            # Get timeout for this phase
            timeout = self._get_phase_timeout(phase)

            # Execute with timeout
            result = await asyncio.wait_for(
                phase_handler(phase_input, state.context),
                timeout=timeout,
            )

            duration = time.time() - start_time

            # Build phase result
            if isinstance(result, PhaseResult):
                phase_result = result
                phase_result.duration = duration
            else:
                phase_result = PhaseResult(
                    phase=phase,
                    success=True,
                    confidence=0.85,  # Default confidence
                    output=result,
                    duration=duration,
                )

            # Check if learning should be triggered
            if (
                self.config.learning_enabled
                and phase_result.confidence >= self.config.learning_threshold
            ):
                phase_result.should_learn = True
                self._stats["learning_triggers"] += 1
                if self.callbacks.on_learning_trigger:
                    try:
                        self.callbacks.on_learning_trigger(task_id, {
                            "phase": phase.name,
                            "confidence": phase_result.confidence,
                            "output": phase_result.output,
                        })
                    except Exception as e:
                        self.logger.error(f"[PhaseManager] Learning callback error: {e}")

            # Invoke phase complete callback
            if self.callbacks.on_phase_complete:
                try:
                    self.callbacks.on_phase_complete(task_id, phase, phase_result)
                except Exception as e:
                    self.logger.error(f"[PhaseManager] Phase complete callback error: {e}")

            return phase_result

        except asyncio.TimeoutError:
            duration = time.time() - start_time
            error_msg = f"Phase {phase.name} timed out after {duration:.2f}s"
            self.logger.error(f"[PhaseManager] {error_msg}")

            state.errors.append({
                "phase": phase.name,
                "error": error_msg,
                "timestamp": time.time(),
            })

            if self.callbacks.on_phase_error:
                try:
                    self.callbacks.on_phase_error(
                        task_id, phase, asyncio.TimeoutError(error_msg)
                    )
                except Exception as e:
                    self.logger.error(f"[PhaseManager] Phase error callback error: {e}")

            return PhaseResult(
                phase=phase,
                success=False,
                confidence=0.0,
                output=None,
                duration=duration,
                error=error_msg,
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)
            self.logger.error(f"[PhaseManager] Phase {phase.name} error: {error_msg}")

            state.errors.append({
                "phase": phase.name,
                "error": error_msg,
                "timestamp": time.time(),
            })

            if self.callbacks.on_phase_error:
                try:
                    self.callbacks.on_phase_error(task_id, phase, e)
                except Exception as cb_error:
                    self.logger.error(f"[PhaseManager] Phase error callback error: {cb_error}")

            return PhaseResult(
                phase=phase,
                success=False,
                confidence=0.0,
                output=None,
                duration=duration,
                error=error_msg,
            )

    def _get_phase_timeout(self, phase: Phase) -> float:
        """Get timeout for a phase."""
        timeouts = {
            Phase.ANALYZING: self.config.analysis_timeout,
            Phase.PLANNING: self.config.planning_timeout,
            Phase.EXECUTING: self.config.execution_timeout,
            Phase.REFLECTING: self.config.reflection_timeout,
            Phase.LEARNING: self.config.reflection_timeout,
        }
        return timeouts.get(phase, 60.0)

    # =========================================================================
    # Checkpoints
    # =========================================================================

    async def create_checkpoint(
        self,
        task_id: str,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[PhaseCheckpoint]:
        """Create a checkpoint for the current phase."""
        state = self._states.get(task_id)
        if not state:
            return None

        if len(state.checkpoints) >= self.config.max_checkpoints_per_task:
            # Remove oldest checkpoint
            state.checkpoints.pop(0)

        checkpoint = PhaseCheckpoint(
            checkpoint_id=str(uuid.uuid4()),
            task_id=task_id,
            phase=state.current_phase,
            timestamp=time.time(),
            confidence=state.phase_confidence,
            context=dict(state.context),  # Copy
            metadata=metadata or {"reason": reason},
        )

        state.checkpoints.append(checkpoint)
        self._stats["checkpoints_created"] += 1

        if self.callbacks.on_checkpoint_created:
            try:
                self.callbacks.on_checkpoint_created(checkpoint)
            except Exception as e:
                self.logger.error(f"[PhaseManager] Checkpoint callback error: {e}")

        self.logger.debug(
            f"[PhaseManager] Checkpoint created for {task_id} at {state.current_phase.name}"
        )

        return checkpoint

    async def restore_checkpoint(
        self,
        task_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> bool:
        """Restore state from a checkpoint."""
        state = self._states.get(task_id)
        if not state or not state.checkpoints:
            return False

        # Find checkpoint
        if checkpoint_id:
            checkpoint = next(
                (cp for cp in state.checkpoints if cp.checkpoint_id == checkpoint_id),
                None,
            )
        else:
            # Use most recent
            checkpoint = state.checkpoints[-1]

        if not checkpoint:
            return False

        # Restore state
        state.current_phase = checkpoint.phase
        state.phase_confidence = checkpoint.confidence
        state.context = dict(checkpoint.context)
        state.updated_at = time.time()

        self.logger.info(
            f"[PhaseManager] Restored {task_id} to checkpoint at {checkpoint.phase.name}"
        )

        return True

    # =========================================================================
    # Task Cleanup
    # =========================================================================

    async def complete_task(self, task_id: str) -> Optional[PhaseState]:
        """Mark a task as complete and clean up."""
        state = self._states.get(task_id)
        if not state:
            return None

        if state.current_phase != Phase.COMPLETED:
            await self.transition(task_id, PhaseTransition.LEARN_COMPLETE)

        final_state = state
        del self._states[task_id]

        self.logger.info(f"[PhaseManager] Task {task_id} completed and cleaned up")

        return final_state

    async def fail_task(
        self,
        task_id: str,
        error: str,
    ) -> Optional[PhaseState]:
        """Mark a task as failed."""
        state = self._states.get(task_id)
        if not state:
            return None

        state.errors.append({
            "phase": state.current_phase.name,
            "error": error,
            "timestamp": time.time(),
            "final": True,
        })

        await self.transition(task_id, PhaseTransition.FAIL)

        self.logger.error(f"[PhaseManager] Task {task_id} failed: {error}")

        return state

    # =========================================================================
    # Statistics and Introspection
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get phase manager statistics."""
        return {
            **self._stats,
            "active_tasks": len(self._states),
            "tasks_by_phase": self._count_by_phase(),
        }

    def _count_by_phase(self) -> Dict[str, int]:
        """Count tasks by current phase."""
        counts: Dict[str, int] = {}
        for state in self._states.values():
            phase_name = state.current_phase.name
            counts[phase_name] = counts.get(phase_name, 0) + 1
        return counts

    def get_active_tasks(self) -> List[str]:
        """Get list of active task IDs."""
        return list(self._states.keys())

    @property
    def is_ready(self) -> bool:
        """Check if manager is ready."""
        return self._initialized


# =============================================================================
# Module-level Singleton Access
# =============================================================================

_phase_manager_instance: Optional[LangGraphPhaseManager] = None


def get_phase_manager() -> Optional[LangGraphPhaseManager]:
    """Get the global phase manager instance."""
    return _phase_manager_instance


def set_phase_manager(manager: LangGraphPhaseManager) -> None:
    """Set the global phase manager instance."""
    global _phase_manager_instance
    _phase_manager_instance = manager


async def start_phase_manager(
    config: Optional[PhaseManagerConfig] = None,
    callbacks: Optional[PhaseCallbacks] = None,
) -> LangGraphPhaseManager:
    """Start and initialize a new phase manager."""
    global _phase_manager_instance

    if _phase_manager_instance is not None:
        return _phase_manager_instance

    manager = LangGraphPhaseManager(config=config, callbacks=callbacks)
    await manager.initialize()
    _phase_manager_instance = manager

    return manager


async def stop_phase_manager() -> None:
    """Stop the global phase manager."""
    global _phase_manager_instance

    if _phase_manager_instance is not None:
        await _phase_manager_instance.shutdown()
        _phase_manager_instance = None
