"""
Intervention Orchestrator
=========================

Provides proactive assistance system for autonomous execution:
- User state monitoring
- Intervention timing optimization
- Effectiveness learning
- Integration with existing InterventionDecisionEngine

v1.0: Initial implementation with intervention timing and effectiveness tracking.

Author: JARVIS AI System
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class InterventionConfig:
    """Configuration for the Intervention Orchestrator."""

    # Intervention settings
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_INTERVENTION_ENABLED", "true").lower() == "true"
    )
    min_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_INTERVENTION_MIN_INTERVAL", "10.0"))
    )
    max_queue_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_INTERVENTION_MAX_QUEUE", "5"))
    )

    # Timing settings
    optimal_timing_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_INTERVENTION_TIMING", "true").lower() == "true"
    )
    idle_detection_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_IDLE_THRESHOLD", "5.0"))
    )

    # Learning settings
    learn_effectiveness: bool = field(
        default_factory=lambda: os.getenv("JARVIS_INTERVENTION_LEARN", "true").lower() == "true"
    )
    min_samples_for_learning: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_LEARNING_SAMPLES", "10"))
    )

    # Priority settings
    urgent_priority_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_URGENT_PRIORITY", "0.9"))
    )


# =============================================================================
# Intervention Types
# =============================================================================


class InterventionType(Enum):
    """Types of intervention."""

    SUGGESTION = auto()  # Suggest an action
    CLARIFICATION = auto()  # Ask for clarification
    CONFIRMATION = auto()  # Confirm before proceeding
    WARNING = auto()  # Warn about potential issue
    NOTIFICATION = auto()  # Inform about status
    ASSISTANCE = auto()  # Offer help
    COMPLETION = auto()  # Task completion message


class InterventionPriority(Enum):
    """Priority levels for interventions."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


class InterventionResult(Enum):
    """Result of an intervention."""

    ACCEPTED = auto()  # User accepted/followed suggestion
    REJECTED = auto()  # User rejected/ignored
    MODIFIED = auto()  # User modified the suggestion
    TIMED_OUT = auto()  # No response
    DEFERRED = auto()  # Postponed for later


@dataclass
class Intervention:
    """A single intervention item."""

    intervention_id: str
    intervention_type: InterventionType
    priority: InterventionPriority
    message: str
    context: Dict[str, Any]
    created_at: float
    expires_at: Optional[float]
    delivered: bool = False
    delivered_at: Optional[float] = None
    result: Optional[InterventionResult] = None
    result_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        """Check if intervention has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "intervention_id": self.intervention_id,
            "type": self.intervention_type.name,
            "priority": self.priority.name,
            "message": self.message,
            "delivered": self.delivered,
            "result": self.result.name if self.result else None,
            "created_at": self.created_at,
        }


@dataclass
class UserState:
    """Current user state for intervention timing."""

    is_idle: bool = False
    idle_duration: float = 0.0
    last_activity: float = field(default_factory=time.time)
    current_focus: str = ""
    in_task: bool = False
    task_progress: float = 0.0
    recent_interventions: int = 0
    last_intervention: Optional[float] = None


@dataclass
class EffectivenessRecord:
    """Record of intervention effectiveness."""

    intervention_type: InterventionType
    context_type: str
    result: InterventionResult
    response_time: float
    timestamp: float


# =============================================================================
# Intervention Orchestrator
# =============================================================================


class InterventionOrchestrator:
    """
    Orchestrates proactive interventions for autonomous tasks.

    Features:
    - Intelligent intervention timing
    - Priority-based queue management
    - User state monitoring
    - Effectiveness learning
    - Integration with TTS for delivery
    """

    def __init__(
        self,
        config: Optional[InterventionConfig] = None,
        tts_callback: Optional[Callable[[str], Any]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the intervention orchestrator."""
        self.config = config or InterventionConfig()
        self.tts_callback = tts_callback
        self.logger = logger or logging.getLogger(__name__)

        # Intervention queue
        self._queue: List[Intervention] = []
        self._pending: Dict[str, Intervention] = {}

        # User state
        self._user_state = UserState()

        # Effectiveness tracking
        self._effectiveness: List[EffectivenessRecord] = []
        self._type_effectiveness: Dict[InterventionType, Dict[str, float]] = {}

        # Processing
        self._processor_task: Optional[asyncio.Task] = None
        self._paused = False

        # Callbacks
        self._delivery_callbacks: List[Callable[[Intervention], None]] = []
        self._result_callbacks: List[Callable[[Intervention], None]] = []

        # Statistics
        self._stats = {
            "total_interventions": 0,
            "delivered": 0,
            "accepted": 0,
            "rejected": 0,
            "timed_out": 0,
            "deferred": 0,
            "expired": 0,
        }

        self._initialized = False
        self._lock = asyncio.Lock()
        self._intervention_counter = 0

    async def initialize(self) -> bool:
        """Initialize the orchestrator."""
        if self._initialized:
            return True

        try:
            self.logger.info("[Intervention] Initializing Intervention Orchestrator...")

            # Start processor if enabled
            if self.config.enabled:
                self._processor_task = asyncio.create_task(self._process_loop())

            self._initialized = True
            self.logger.info("[Intervention] ✓ Orchestrator initialized")
            return True

        except Exception as e:
            self.logger.error(f"[Intervention] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        if not self._initialized:
            return

        self.logger.info("[Intervention] Shutting down...")

        # Stop processor
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        self._initialized = False
        self.logger.info("[Intervention] ✓ Shutdown complete")

    # =========================================================================
    # Intervention Creation
    # =========================================================================

    async def queue_intervention(
        self,
        intervention_type: InterventionType,
        message: str,
        priority: InterventionPriority = InterventionPriority.NORMAL,
        context: Optional[Dict[str, Any]] = None,
        expires_in: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Queue a new intervention."""
        async with self._lock:
            self._intervention_counter += 1
            intervention_id = f"int_{self._intervention_counter}_{int(time.time())}"

            intervention = Intervention(
                intervention_id=intervention_id,
                intervention_type=intervention_type,
                priority=priority,
                message=message,
                context=context or {},
                created_at=time.time(),
                expires_at=time.time() + expires_in if expires_in else None,
                metadata=metadata or {},
            )

            # Check queue size
            if len(self._queue) >= self.config.max_queue_size:
                # Remove lowest priority expired items
                self._queue = [i for i in self._queue if not i.is_expired()]
                if len(self._queue) >= self.config.max_queue_size:
                    # Remove lowest priority
                    self._queue.sort(key=lambda x: x.priority.value)
                    self._queue.pop(0)

            self._queue.append(intervention)
            self._stats["total_interventions"] += 1

            self.logger.debug(
                f"[Intervention] Queued: {intervention_type.name} "
                f"priority={priority.name}"
            )

            return intervention_id

    async def queue_suggestion(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        priority: InterventionPriority = InterventionPriority.NORMAL,
    ) -> str:
        """Queue a suggestion intervention."""
        return await self.queue_intervention(
            InterventionType.SUGGESTION,
            message,
            priority,
            context,
        )

    async def queue_confirmation(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        priority: InterventionPriority = InterventionPriority.HIGH,
        expires_in: float = 60.0,
    ) -> str:
        """Queue a confirmation intervention."""
        return await self.queue_intervention(
            InterventionType.CONFIRMATION,
            message,
            priority,
            context,
            expires_in,
        )

    async def queue_warning(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        priority: InterventionPriority = InterventionPriority.HIGH,
    ) -> str:
        """Queue a warning intervention."""
        return await self.queue_intervention(
            InterventionType.WARNING,
            message,
            priority,
            context,
        )

    async def queue_notification(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        priority: InterventionPriority = InterventionPriority.LOW,
    ) -> str:
        """Queue a notification intervention."""
        return await self.queue_intervention(
            InterventionType.NOTIFICATION,
            message,
            priority,
            context,
        )

    # =========================================================================
    # Processing
    # =========================================================================

    async def _process_loop(self) -> None:
        """Main processing loop for interventions."""
        while True:
            try:
                if not self._paused and self._queue:
                    await self._process_next()

                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[Intervention] Processing error: {e}")
                await asyncio.sleep(1.0)

    async def _process_next(self) -> None:
        """Process the next intervention in queue."""
        async with self._lock:
            if not self._queue:
                return

            # Check timing
            if not self._is_good_timing():
                return

            # Clean expired
            self._queue = [i for i in self._queue if not i.is_expired()]
            for expired in [i for i in self._queue if i.is_expired()]:
                self._stats["expired"] += 1

            if not self._queue:
                return

            # Sort by priority (highest first) and age (oldest first)
            self._queue.sort(
                key=lambda x: (-x.priority.value, x.created_at),
            )

            # Get next intervention
            intervention = self._queue.pop(0)

            # Deliver
            await self._deliver(intervention)

    def _is_good_timing(self) -> bool:
        """Check if now is a good time to intervene."""
        if not self.config.optimal_timing_enabled:
            return True

        # Check minimum interval
        if self._user_state.last_intervention:
            elapsed = time.time() - self._user_state.last_intervention
            if elapsed < self.config.min_interval:
                return False

        # Check if user is idle (good time)
        if self._user_state.is_idle:
            return True

        # Check if urgent
        if self._queue:
            top = max(self._queue, key=lambda x: x.priority.value)
            if top.priority.value >= InterventionPriority.URGENT.value:
                return True

        # Check recent intervention count
        if self._user_state.recent_interventions >= 3:
            return False

        return True

    async def _deliver(self, intervention: Intervention) -> None:
        """Deliver an intervention."""
        intervention.delivered = True
        intervention.delivered_at = time.time()
        self._stats["delivered"] += 1

        # Update user state
        self._user_state.last_intervention = time.time()
        self._user_state.recent_interventions += 1

        # Move to pending
        self._pending[intervention.intervention_id] = intervention

        # Notify callbacks
        for callback in self._delivery_callbacks:
            try:
                callback(intervention)
            except Exception as e:
                self.logger.error(f"[Intervention] Delivery callback error: {e}")

        # Deliver via TTS if available
        if self.tts_callback:
            try:
                await self.tts_callback(intervention.message)
            except Exception as e:
                self.logger.error(f"[Intervention] TTS delivery error: {e}")

        self.logger.info(
            f"[Intervention] Delivered: {intervention.intervention_type.name} - "
            f"{intervention.message[:50]}..."
        )

    # =========================================================================
    # Results
    # =========================================================================

    async def report_result(
        self,
        intervention_id: str,
        result: InterventionResult,
        response_time: Optional[float] = None,
    ) -> None:
        """Report the result of an intervention."""
        async with self._lock:
            intervention = self._pending.get(intervention_id)
            if not intervention:
                self.logger.warning(
                    f"[Intervention] Result for unknown intervention: {intervention_id}"
                )
                return

            intervention.result = result
            intervention.result_at = time.time()

            # Calculate response time
            if response_time is None and intervention.delivered_at:
                response_time = time.time() - intervention.delivered_at

            # Update stats
            if result == InterventionResult.ACCEPTED:
                self._stats["accepted"] += 1
            elif result == InterventionResult.REJECTED:
                self._stats["rejected"] += 1
            elif result == InterventionResult.TIMED_OUT:
                self._stats["timed_out"] += 1
            elif result == InterventionResult.DEFERRED:
                self._stats["deferred"] += 1

            # Record for learning
            if self.config.learn_effectiveness:
                self._record_effectiveness(intervention, result, response_time or 0.0)

            # Notify callbacks
            for callback in self._result_callbacks:
                try:
                    callback(intervention)
                except Exception as e:
                    self.logger.error(f"[Intervention] Result callback error: {e}")

            # Remove from pending
            del self._pending[intervention_id]

    def _record_effectiveness(
        self,
        intervention: Intervention,
        result: InterventionResult,
        response_time: float,
    ) -> None:
        """Record effectiveness for learning."""
        record = EffectivenessRecord(
            intervention_type=intervention.intervention_type,
            context_type=intervention.context.get("type", "general"),
            result=result,
            response_time=response_time,
            timestamp=time.time(),
        )

        self._effectiveness.append(record)

        # Keep only recent records
        if len(self._effectiveness) > 1000:
            self._effectiveness = self._effectiveness[-500:]

        # Update type effectiveness
        self._update_type_effectiveness()

    def _update_type_effectiveness(self) -> None:
        """Update effectiveness scores by type."""
        if len(self._effectiveness) < self.config.min_samples_for_learning:
            return

        # Group by type
        by_type: Dict[InterventionType, List[EffectivenessRecord]] = {}
        for record in self._effectiveness:
            if record.intervention_type not in by_type:
                by_type[record.intervention_type] = []
            by_type[record.intervention_type].append(record)

        # Calculate effectiveness
        for int_type, records in by_type.items():
            accepted = sum(1 for r in records if r.result == InterventionResult.ACCEPTED)
            total = len(records)

            self._type_effectiveness[int_type] = {
                "acceptance_rate": accepted / total if total > 0 else 0.0,
                "avg_response_time": sum(r.response_time for r in records) / total if total > 0 else 0.0,
                "sample_count": total,
            }

    # =========================================================================
    # User State
    # =========================================================================

    async def update_user_state(
        self,
        is_idle: Optional[bool] = None,
        current_focus: Optional[str] = None,
        in_task: Optional[bool] = None,
        task_progress: Optional[float] = None,
    ) -> None:
        """Update user state information."""
        now = time.time()

        if is_idle is not None:
            if is_idle and not self._user_state.is_idle:
                self._user_state.idle_duration = 0.0
            elif is_idle:
                self._user_state.idle_duration = now - self._user_state.last_activity
            else:
                self._user_state.last_activity = now
            self._user_state.is_idle = is_idle

        if current_focus is not None:
            self._user_state.current_focus = current_focus

        if in_task is not None:
            self._user_state.in_task = in_task

        if task_progress is not None:
            self._user_state.task_progress = task_progress

        # Decay recent intervention count over time
        if self._user_state.last_intervention:
            elapsed = now - self._user_state.last_intervention
            if elapsed > 60:
                self._user_state.recent_interventions = max(
                    0, self._user_state.recent_interventions - 1
                )

    # =========================================================================
    # Control
    # =========================================================================

    def pause(self) -> None:
        """Pause intervention processing."""
        self._paused = True

    def resume(self) -> None:
        """Resume intervention processing."""
        self._paused = False

    async def clear_queue(self) -> None:
        """Clear all queued interventions."""
        async with self._lock:
            self._queue.clear()

    def register_delivery_callback(
        self,
        callback: Callable[[Intervention], None],
    ) -> None:
        """Register callback for intervention delivery."""
        self._delivery_callbacks.append(callback)

    def register_result_callback(
        self,
        callback: Callable[[Intervention], None],
    ) -> None:
        """Register callback for intervention results."""
        self._result_callbacks.append(callback)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        return {
            **self._stats,
            "queue_size": len(self._queue),
            "pending_count": len(self._pending),
            "user_idle": self._user_state.is_idle,
            "paused": self._paused,
            "type_effectiveness": {
                t.name: e for t, e in self._type_effectiveness.items()
            },
        }

    def get_queue_status(self) -> List[Dict[str, Any]]:
        """Get current queue status."""
        return [i.to_dict() for i in self._queue]

    def get_pending_status(self) -> List[Dict[str, Any]]:
        """Get pending interventions."""
        return [i.to_dict() for i in self._pending.values()]

    @property
    def is_ready(self) -> bool:
        """Check if orchestrator is ready."""
        return self._initialized


# =============================================================================
# Module-level Singleton Access
# =============================================================================

_orchestrator_instance: Optional[InterventionOrchestrator] = None


def get_intervention_orchestrator() -> Optional[InterventionOrchestrator]:
    """Get the global intervention orchestrator."""
    return _orchestrator_instance


def set_intervention_orchestrator(orchestrator: InterventionOrchestrator) -> None:
    """Set the global intervention orchestrator."""
    global _orchestrator_instance
    _orchestrator_instance = orchestrator


async def start_intervention_orchestrator(
    config: Optional[InterventionConfig] = None,
    tts_callback: Optional[Callable[[str], Any]] = None,
) -> InterventionOrchestrator:
    """Start and initialize the intervention orchestrator."""
    global _orchestrator_instance

    if _orchestrator_instance is not None:
        return _orchestrator_instance

    orchestrator = InterventionOrchestrator(
        config=config,
        tts_callback=tts_callback,
    )
    await orchestrator.initialize()
    _orchestrator_instance = orchestrator

    return orchestrator


async def stop_intervention_orchestrator() -> None:
    """Stop the global intervention orchestrator."""
    global _orchestrator_instance

    if _orchestrator_instance is not None:
        await _orchestrator_instance.shutdown()
        _orchestrator_instance = None
