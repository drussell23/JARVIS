#!/usr/bin/env python3
"""
Predictive Pre-computation Engine
Purpose: Eliminate latency by computing responses before they're needed

Markov Chain-based prediction system with:
- State space modeling
- Transition probability tracking
- Speculative execution
- Dynamic learning and adaptation

Memory Allocation: Adaptive based on macOS memory pressure
- GREEN: 150MB (60MB + 40MB + 50MB)
- YELLOW: 75MB (50% reduction)
- RED: 37.5MB (75% reduction)
"""

import asyncio
import hashlib
import heapq
import logging
import os
import pickle  # nosec B403 - Used only for internal cache serialization, not untrusted data
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import scipy.sparse as sp

from .semantic_cache_lsh import SemanticCacheWithLSH

# Integration imports
from .temporal_context_engine import TemporalContextEngine
from .workflow_pattern_engine import WorkflowPatternEngine

logger = logging.getLogger(__name__)

# Base memory allocation (will be scaled by pressure)
BASE_MEMORY_LIMITS = {
    "markov_model": 60 * 1024 * 1024,  # 60MB baseline
    "prediction_queue": 40 * 1024 * 1024,  # 40MB baseline
    "result_cache": 50 * 1024 * 1024,  # 50MB baseline
}


def get_memory_limits_predictive(memory_manager=None) -> Dict[str, int]:
    """Get memory limits adjusted for current memory pressure"""
    if not memory_manager:
        return BASE_MEMORY_LIMITS.copy()

    try:
        # Import here to avoid circular dependency
        import sys

        vision_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if vision_dir not in sys.path:
            sys.path.insert(0, vision_dir)

        from macos_memory_manager import MemoryPressure

        pressure = memory_manager.current_pressure

        # Scale based on pressure
        if pressure == MemoryPressure.RED:
            scale_factor = 0.25  # 25% under critical pressure
        elif pressure == MemoryPressure.YELLOW:
            scale_factor = 0.5  # 50% under moderate pressure
        else:  # GREEN or UNKNOWN
            scale_factor = 1.0  # Full capacity

        return {key: int(value * scale_factor) for key, value in BASE_MEMORY_LIMITS.items()}
    except Exception as e:
        logger.warning(f"Could not get memory pressure, using baseline: {e}")
        return BASE_MEMORY_LIMITS.copy()


# Initialize with baseline
MEMORY_LIMITS = BASE_MEMORY_LIMITS.copy()


class StateType(Enum):
    """Types of states in the Markov chain"""

    APPLICATION = auto()  # Application-specific states
    USER_ACTION = auto()  # User interaction states
    TIME_CONTEXT = auto()  # Time-based states
    GOAL_STATE = auto()  # Goal-oriented states
    WORKFLOW = auto()  # Workflow states
    SYSTEM = auto()  # System-level states


@dataclass
class StateVector:
    """Multi-dimensional state representation"""

    app_id: str
    app_state: str
    user_action: Optional[str] = None
    time_context: Optional[str] = None
    goal_context: Optional[str] = None
    workflow_phase: Optional[str] = None
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_tuple(self) -> Tuple:
        """Convert to hashable tuple for state indexing"""
        return (
            self.app_id,
            self.app_state,
            self.user_action or "",
            self.time_context or "",
            self.goal_context or "",
            self.workflow_phase or "",
        )

    def similarity(self, other: "StateVector") -> float:
        """Calculate similarity between states"""
        score = 0.0
        weights = {
            "app_id": 0.3,
            "app_state": 0.25,
            "user_action": 0.15,
            "time_context": 0.1,
            "goal_context": 0.15,
            "workflow_phase": 0.05,
        }

        if self.app_id == other.app_id:
            score += weights["app_id"]
        if self.app_state == other.app_state:
            score += weights["app_state"]
        if self.user_action == other.user_action:
            score += weights["user_action"]
        if self.time_context == other.time_context:
            score += weights["time_context"]
        if self.goal_context == other.goal_context:
            score += weights["goal_context"]
        if self.workflow_phase == other.workflow_phase:
            score += weights["workflow_phase"]

        return score


@dataclass
class Transition:
    """State transition with probability and metadata"""

    from_state: StateVector
    to_state: StateVector
    probability: float
    count: int = 1
    temporal_factor: float = 1.0
    confidence: float = 0.0
    last_observed: datetime = field(default_factory=datetime.now)
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PredictionTask:
    """Speculative execution task"""

    id: str
    state: StateVector
    predicted_next_states: List[Tuple[StateVector, float]]  # (state, probability)
    priority: float
    created_at: datetime = field(default_factory=datetime.now)
    deadline: Optional[datetime] = None
    computation_function: Optional[callable] = None
    resources_allocated: Dict[str, float] = field(default_factory=dict)
    result: Optional[Any] = None
    status: str = "pending"  # pending, executing, completed, failed

    def __lt__(self, other):
        """For priority queue ordering (higher priority first)"""
        return self.priority > other.priority


class TransitionMatrix:
    """Sparse representation of state transitions"""

    def __init__(self, max_states: int = 10000):
        self.max_states = max_states
        self.state_to_idx: Dict[Tuple, int] = {}
        self.idx_to_state: Dict[int, StateVector] = {}
        self.next_idx = 0

        # Sparse matrix for efficient storage
        self.transition_counts = sp.dok_matrix((max_states, max_states), dtype=np.float32)
        self.transition_probs = sp.dok_matrix((max_states, max_states), dtype=np.float32)

        # Temporal factors
        self.temporal_weights = sp.dok_matrix((max_states, max_states), dtype=np.float32)

        # Confidence scores
        self.confidence_scores = sp.dok_matrix((max_states, max_states), dtype=np.float32)

        # State metadata
        self.state_metadata: Dict[int, Dict[str, Any]] = {}

        # Lock for thread safety
        self._lock = threading.RLock()

    def add_state(self, state: StateVector) -> int:
        """Add state to matrix if not exists"""
        state_tuple = state.to_tuple()

        with self._lock:
            if state_tuple not in self.state_to_idx:
                if self.next_idx >= self.max_states:
                    # Evict least used states
                    self._evict_states()

                idx = self.next_idx
                self.state_to_idx[state_tuple] = idx
                self.idx_to_state[idx] = state
                self.state_metadata[idx] = {
                    "first_seen": datetime.now(),
                    "last_seen": datetime.now(),
                    "total_visits": 0,
                }
                self.next_idx += 1
                return idx

            idx = self.state_to_idx[state_tuple]
            self.state_metadata[idx]["last_seen"] = datetime.now()
            self.state_metadata[idx]["total_visits"] += 1
            return idx

    def add_transition(
        self, from_state: StateVector, to_state: StateVector, temporal_factor: float = 1.0
    ):
        """Record state transition"""
        with self._lock:
            from_idx = self.add_state(from_state)
            to_idx = self.add_state(to_state)

            # Update counts
            self.transition_counts[from_idx, to_idx] += 1

            # Update temporal weights
            self.temporal_weights[from_idx, to_idx] = (
                0.9 * self.temporal_weights[from_idx, to_idx] + 0.1 * temporal_factor
            )

            # Recalculate probabilities for this state
            self._update_probabilities(from_idx)

    def _update_probabilities(self, state_idx: int):
        """Update transition probabilities for a state"""
        # Get all transitions from this state
        transitions = self.transition_counts.getrow(state_idx).toarray().flatten()
        temporal = self.temporal_weights.getrow(state_idx).toarray().flatten()

        # Apply temporal weighting
        weighted_counts = transitions * (0.7 + 0.3 * temporal)

        # Calculate probabilities
        total = np.sum(weighted_counts)
        if total > 0:
            probs = weighted_counts / total

            # Update probability matrix
            for i, prob in enumerate(probs):
                if prob > 0:
                    self.transition_probs[state_idx, i] = prob

                    # Calculate confidence based on count
                    count = transitions[i]
                    confidence = min(1.0, count / 10.0)  # Max confidence at 10 observations
                    self.confidence_scores[state_idx, i] = confidence

    def get_predictions(
        self, state: StateVector, top_k: int = 5
    ) -> List[Tuple[StateVector, float, float]]:
        """Get top-k predicted next states with probabilities and confidence"""
        state_tuple = state.to_tuple()

        with self._lock:
            if state_tuple not in self.state_to_idx:
                return []

            state_idx = self.state_to_idx[state_tuple]

            # Get probabilities
            probs = self.transition_probs.getrow(state_idx).toarray().flatten()
            confidences = self.confidence_scores.getrow(state_idx).toarray().flatten()

            # Get top-k indices
            top_indices = np.argsort(probs)[-top_k:][::-1]

            predictions = []
            for idx in top_indices:
                if idx in self.idx_to_state and probs[idx] > 0:
                    predictions.append(
                        (self.idx_to_state[idx], float(probs[idx]), float(confidences[idx]))
                    )

            return predictions

    def _evict_states(self):
        """Evict least recently used states"""
        # Sort states by last access time
        states_by_access = sorted(self.state_metadata.items(), key=lambda x: x[1]["last_seen"])

        # Evict oldest 10%
        num_to_evict = int(0.1 * len(states_by_access))

        for idx, _ in states_by_access[:num_to_evict]:
            if idx in self.idx_to_state:
                state = self.idx_to_state[idx]
                state_tuple = state.to_tuple()

                # Remove from mappings
                del self.state_to_idx[state_tuple]
                del self.idx_to_state[idx]
                del self.state_metadata[idx]

                # Clear matrix rows/cols
                self.transition_counts[idx, :] = 0
                self.transition_counts[:, idx] = 0
                self.transition_probs[idx, :] = 0
                self.transition_probs[:, idx] = 0
                self.temporal_weights[idx, :] = 0
                self.temporal_weights[:, idx] = 0
                self.confidence_scores[idx, :] = 0
                self.confidence_scores[:, idx] = 0


class PredictionQueue:
    """Priority queue for prediction tasks with resource management"""

    def __init__(self, max_size_bytes: int):
        self.max_size_bytes = max_size_bytes
        self.current_size_bytes = 0
        self.queue: List[PredictionTask] = []
        self.task_map: Dict[str, PredictionTask] = {}
        if _HAS_MANAGED_EXECUTOR:

            self.executor = ManagedThreadPoolExecutor(max_workers=4, name='pool')

        else:

            self.executor = ThreadPoolExecutor(max_workers=4)
        self.active_tasks: Dict[str, Future] = {}
        self._lock = threading.Lock()

        # Resource allocation
        self.resource_pool = {"cpu": 1.0, "memory": 1.0, "io": 1.0}

    def add_task(self, task: PredictionTask) -> bool:
        """Add prediction task to queue"""
        task_size = self._estimate_task_size(task)

        with self._lock:
            # Check if we have space
            if self.current_size_bytes + task_size > self.max_size_bytes:
                self._evict_low_priority_tasks(task_size)

            if self.current_size_bytes + task_size <= self.max_size_bytes:
                heapq.heappush(self.queue, task)
                self.task_map[task.id] = task
                self.current_size_bytes += task_size
                return True

            return False

    def get_next_task(self) -> Optional[PredictionTask]:
        """Get highest priority task ready for execution"""
        with self._lock:
            while self.queue:
                task = heapq.heappop(self.queue)

                if task.deadline and datetime.now() > task.deadline:
                    # Task expired
                    task.status = "expired"
                    del self.task_map[task.id]
                    continue

                # Check resource availability
                if self._can_allocate_resources(task):
                    self._allocate_resources(task)
                    task.status = "executing"
                    return task
                else:
                    # Put back in queue
                    heapq.heappush(self.queue, task)
                    break

            return None

    def _estimate_task_size(self, task: PredictionTask) -> int:
        """Estimate memory size of task"""
        # Basic estimate
        size = 1024  # Base overhead
        size += len(pickle.dumps(task.state))
        size += len(task.predicted_next_states) * 512
        if task.result:
            size += len(pickle.dumps(task.result))
        return size

    def _evict_low_priority_tasks(self, required_space: int):
        """Evict tasks to make space"""
        # Sort by priority (ascending)
        sorted_tasks = sorted(self.queue, key=lambda t: t.priority)

        freed_space = 0
        tasks_to_remove = []

        for task in sorted_tasks:
            if freed_space >= required_space:
                break

            task_size = self._estimate_task_size(task)
            freed_space += task_size
            tasks_to_remove.append(task)

        # Remove tasks
        for task in tasks_to_remove:
            self.queue.remove(task)
            del self.task_map[task.id]
            self.current_size_bytes -= self._estimate_task_size(task)

        # Rebuild heap
        heapq.heapify(self.queue)

    def _can_allocate_resources(self, task: PredictionTask) -> bool:
        """Check if resources available for task"""
        for resource, required in task.resources_allocated.items():
            if self.resource_pool.get(resource, 0) < required:
                return False
        return True

    def _allocate_resources(self, task: PredictionTask):
        """Allocate resources to task"""
        for resource, amount in task.resources_allocated.items():
            self.resource_pool[resource] -= amount

    def _release_resources(self, task: PredictionTask):
        """Release resources from task"""
        for resource, amount in task.resources_allocated.items():
            self.resource_pool[resource] += amount

    async def execute_task(self, task: PredictionTask) -> Any:
        """Execute prediction task"""
        try:
            if task.computation_function:
                # Run in executor to avoid blocking
                result = await asyncio.get_event_loop().run_in_executor(
                    self.executor, task.computation_function, task.state, task.predicted_next_states
                )
                task.result = result
                task.status = "completed"
            else:
                task.status = "no_computation"

            return task.result

        except Exception as e:
            logger.error(f"Task execution failed: {e}")
            task.status = "failed"
            raise
        finally:
            with self._lock:
                self._release_resources(task)
                if task.id in self.task_map:
                    del self.task_map[task.id]
                self.current_size_bytes -= self._estimate_task_size(task)


class LearningSystem:
    """Adaptive learning for prediction accuracy"""

    def __init__(self):
        self.prediction_history: Deque[Tuple[StateVector, StateVector, float]] = deque(maxlen=1000)
        self.accuracy_metrics = {
            "total_predictions": 0,
            "correct_predictions": 0,
            "confidence_calibration": [],
            "state_type_accuracy": defaultdict(lambda: {"correct": 0, "total": 0}),
        }
        self.model_parameters = {
            "temporal_decay": 0.95,
            "confidence_threshold": 0.7,
            "learning_rate": 0.1,
            "drift_threshold": 0.2,
        }
        self._lock = threading.Lock()

    def record_prediction(self, predicted: StateVector, actual: StateVector, confidence: float):
        """Record prediction outcome for learning"""
        with self._lock:
            self.prediction_history.append((predicted, actual, confidence))
            self.accuracy_metrics["total_predictions"] += 1

            # Check if prediction was correct (similarity-based)
            similarity = predicted.similarity(actual)
            if similarity > 0.8:
                self.accuracy_metrics["correct_predictions"] += 1
                self.accuracy_metrics["state_type_accuracy"][predicted.app_id]["correct"] += 1

            self.accuracy_metrics["state_type_accuracy"][predicted.app_id]["total"] += 1
            self.accuracy_metrics["confidence_calibration"].append((confidence, similarity))

            # Detect drift
            if len(self.prediction_history) >= 100:
                self._detect_drift()

    def _detect_drift(self):
        """Detect concept drift in predictions"""
        recent_predictions = list(self.prediction_history)[-100:]
        recent_accuracy = sum(1 for p, a, _ in recent_predictions if p.similarity(a) > 0.8) / len(
            recent_predictions
        )

        overall_accuracy = self.accuracy_metrics["correct_predictions"] / max(
            1, self.accuracy_metrics["total_predictions"]
        )

        if overall_accuracy - recent_accuracy > self.model_parameters["drift_threshold"]:
            logger.warning(
                f"Concept drift detected: {recent_accuracy:.2f} vs {overall_accuracy:.2f}"
            )
            self._adapt_model()

    def _adapt_model(self):
        """Adapt model parameters based on performance"""
        # Adjust confidence threshold based on calibration
        if self.accuracy_metrics["confidence_calibration"]:
            calibration_data = self.accuracy_metrics["confidence_calibration"][-100:]

            # Find optimal threshold
            best_threshold = self.model_parameters["confidence_threshold"]
            best_f1 = 0.0

            for threshold in np.arange(0.5, 0.95, 0.05):
                tp = sum(1 for conf, sim in calibration_data if conf >= threshold and sim > 0.8)
                fp = sum(1 for conf, sim in calibration_data if conf >= threshold and sim <= 0.8)
                fn = sum(1 for conf, sim in calibration_data if conf < threshold and sim > 0.8)

                precision = tp / max(1, tp + fp)
                recall = tp / max(1, tp + fn)
                f1 = 2 * precision * recall / max(0.001, precision + recall)

                if f1 > best_f1:
                    best_f1 = f1
                    best_threshold = float(threshold)

            # Update threshold with learning rate
            self.model_parameters["confidence_threshold"] = (
                0.9 * self.model_parameters["confidence_threshold"] + 0.1 * best_threshold
            )

    def get_accuracy_report(self) -> Dict[str, Any]:
        """Get comprehensive accuracy report"""
        with self._lock:
            total = max(1, self.accuracy_metrics["total_predictions"])

            return {
                "overall_accuracy": self.accuracy_metrics["correct_predictions"] / total,
                "total_predictions": total,
                "confidence_threshold": self.model_parameters["confidence_threshold"],
                "state_type_performance": dict(self.accuracy_metrics["state_type_accuracy"]),
                "recent_trend": self._calculate_trend(),
            }

    def _calculate_trend(self) -> str:
        """Calculate accuracy trend"""
        if len(self.prediction_history) < 20:
            return "insufficient_data"

        # Compare recent vs older predictions
        recent = list(self.prediction_history)[-10:]
        older = list(self.prediction_history)[-20:-10]

        recent_acc = sum(1 for p, a, _ in recent if p.similarity(a) > 0.8) / len(recent)
        older_acc = sum(1 for p, a, _ in older if p.similarity(a) > 0.8) / len(older)

        if recent_acc > older_acc + 0.1:
            return "improving"
        elif recent_acc < older_acc - 0.1:
            return "degrading"
        else:
            return "stable"


class PredictivePrecomputationEngine:
    """Main engine for predictive pre-computation with memory pressure awareness"""

    def __init__(self, memory_manager=None):
        # Memory pressure integration
        self.memory_manager = memory_manager
        self._update_memory_limits()

        # Core components
        self.transition_matrix = TransitionMatrix()
        self.prediction_queue = PredictionQueue(MEMORY_LIMITS["prediction_queue"])
        self.learning_system = LearningSystem()

        # Result cache
        self.result_cache: Dict[str, Tuple[Any, datetime]] = {}
        self.cache_size_bytes = 0
        self.max_cache_size = MEMORY_LIMITS["result_cache"]

        # State tracking
        self.current_state: Optional[StateVector] = None
        self.state_history: Deque[StateVector] = deque(maxlen=100)

        # Integration points
        self.temporal_engine: Optional[TemporalContextEngine] = None
        self.workflow_engine: Optional[WorkflowPatternEngine] = None
        self.semantic_cache: Optional[SemanticCacheWithLSH] = None

        # Background tasks
        self._running = False
        self._prediction_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        # Statistics
        self.stats = {
            "predictions_made": 0,
            "predictions_executed": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "average_prediction_time": 0.0,
        }

        # Register for memory pressure callbacks if manager available
        if self.memory_manager:
            self.memory_manager.register_pressure_callback(self._on_pressure_change)

    def _update_memory_limits(self):
        """Update memory limits based on current pressure"""
        global MEMORY_LIMITS
        if self.memory_manager:
            new_limits = get_memory_limits_predictive(self.memory_manager)
            if new_limits != MEMORY_LIMITS:
                old_total = sum(MEMORY_LIMITS.values()) / 1024 / 1024
                new_total = sum(new_limits.values()) / 1024 / 1024
                logger.info(
                    f"Predictive Engine: Adjusting memory limits based on pressure: "
                    f"{old_total:.0f}MB → {new_total:.0f}MB"
                )
                MEMORY_LIMITS = new_limits

                # Update cache limit
                self.max_cache_size = MEMORY_LIMITS["result_cache"]

    async def _on_pressure_change(self, new_pressure, stats):
        """Callback when memory pressure changes"""
        self._update_memory_limits()

        # If under pressure, aggressively clean cache
        from macos_memory_manager import MemoryPressure

        if new_pressure in (MemoryPressure.YELLOW, MemoryPressure.RED):
            await self._aggressive_cleanup()

    async def _aggressive_cleanup(self):
        """Aggressively clean cache under memory pressure"""
        if not self.result_cache:
            return

        # Get current limit
        current_limit = self.max_cache_size

        # Evict until we're under limit
        removed_count = 0
        while self.cache_size_bytes > current_limit and self.result_cache:
            # Remove oldest entry
            oldest_key = min(self.result_cache.keys(), key=lambda k: self.result_cache[k][1])
            old_result, _ = self.result_cache[oldest_key]
            self.cache_size_bytes -= self._estimate_result_size(old_result)
            del self.result_cache[oldest_key]
            removed_count += 1

        if removed_count > 0:
            logger.info(
                f"Predictive Engine: Aggressive cleanup removed {removed_count} entries, "
                f"cache now {self.cache_size_bytes / 1024 / 1024:.1f}MB"
            )

    async def initialize(self):
        """Initialize engine and start background tasks"""
        self._running = True
        self._prediction_task = asyncio.create_task(self._prediction_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Predictive Pre-computation Engine initialized")

    async def shutdown(self):
        """Shutdown engine"""
        self._running = False
        if self._prediction_task:
            self._prediction_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()

        # Wait for tasks to complete
        await asyncio.gather(self._prediction_task, self._cleanup_task, return_exceptions=True)

        # Shutdown executor
        self.prediction_queue.executor.shutdown(wait=True)

    async def update_state(self, new_state: StateVector):
        """Update current state and generate predictions"""
        # Record transition
        if self.current_state:
            # Calculate temporal factor based on time between states
            if self.state_history:
                time_diff = (
                    datetime.now()
                    - self.state_history[-1].metadata.get("timestamp", datetime.now())
                ).total_seconds()
                temporal_factor = np.exp(-time_diff / 60.0)  # Decay over 1 minute
            else:
                temporal_factor = 1.0

            self.transition_matrix.add_transition(self.current_state, new_state, temporal_factor)

        # Update state
        new_state.metadata["timestamp"] = datetime.now()
        self.current_state = new_state
        self.state_history.append(new_state)

        # Generate predictions
        await self._generate_predictions(new_state)

    async def _generate_predictions(self, state: StateVector):
        """Generate predictions for given state"""
        predictions = self.transition_matrix.get_predictions(state, top_k=5)

        for next_state, probability, confidence in predictions:
            if confidence >= self.learning_system.model_parameters["confidence_threshold"]:
                # Create prediction task
                task_id = hashlib.md5(
                    f"{state.to_tuple()}_{next_state.to_tuple()}_{datetime.now()}".encode(),
                    usedforsecurity=False,
                ).hexdigest()

                task = PredictionTask(
                    id=task_id,
                    state=state,
                    predicted_next_states=[(next_state, probability)],
                    priority=probability * confidence,
                    deadline=datetime.now() + timedelta(seconds=30),
                    computation_function=self._get_computation_function(next_state),
                    resources_allocated={"cpu": 0.1, "memory": 0.05},
                )

                # Add to queue
                if self.prediction_queue.add_task(task):
                    self.stats["predictions_made"] += 1

    def _get_computation_function(self, state: StateVector) -> Optional[callable]:
        """Get computation function for state"""
        # Determine computation based on state type
        if state.app_id == "chrome" and "search" in state.app_state:
            return self._compute_search_results
        elif state.app_id == "vscode" and "completion" in state.app_state:
            return self._compute_code_completion
        elif "save" in state.user_action:
            return self._compute_save_locations
        elif state.goal_context and "navigate" in state.goal_context:
            return self._compute_navigation_targets

        return None

    async def _prediction_loop(self):
        """Background loop for executing predictions"""
        while self._running:
            try:
                # Get next task
                task = self.prediction_queue.get_next_task()

                if task:
                    start_time = time.time()

                    # Execute prediction
                    try:
                        result = await self.prediction_queue.execute_task(task)

                        if result:
                            # Cache result
                            self._cache_result(task, result)

                        self.stats["predictions_executed"] += 1

                        # Update timing
                        elapsed = time.time() - start_time
                        self.stats["average_prediction_time"] = (
                            0.9 * self.stats["average_prediction_time"] + 0.1 * elapsed
                        )

                    except Exception as e:
                        logger.error(f"Prediction execution failed: {e}")

                # Small delay to prevent busy waiting
                await asyncio.sleep(0.01)

            except Exception as e:
                logger.error(f"Prediction loop error: {e}")
                await asyncio.sleep(1)

    async def _cleanup_loop(self):
        """Background loop for cleanup tasks"""
        while self._running:
            try:
                # Clean expired cache entries
                now = datetime.now()
                expired_keys = [
                    key
                    for key, (_, timestamp) in self.result_cache.items()
                    if now - timestamp > timedelta(minutes=5)
                ]

                for key in expired_keys:
                    result, _ = self.result_cache[key]
                    self.cache_size_bytes -= self._estimate_result_size(result)
                    del self.result_cache[key]

                # Sleep for cleanup interval
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")
                await asyncio.sleep(60)

    def _cache_result(self, task: PredictionTask, result: Any):
        """Cache pre-computed result"""
        # Create cache key
        cache_key = hashlib.md5(
            f"{task.state.to_tuple()}_{task.predicted_next_states[0][0].to_tuple()}".encode(),
            usedforsecurity=False,
        ).hexdigest()

        # Estimate size
        result_size = self._estimate_result_size(result)

        # Evict if needed
        while self.cache_size_bytes + result_size > self.max_cache_size and self.result_cache:
            # Remove oldest entry
            oldest_key = min(self.result_cache.keys(), key=lambda k: self.result_cache[k][1])
            old_result, _ = self.result_cache[oldest_key]
            self.cache_size_bytes -= self._estimate_result_size(old_result)
            del self.result_cache[oldest_key]

        # Store result
        self.result_cache[cache_key] = (result, datetime.now())
        self.cache_size_bytes += result_size

    def _estimate_result_size(self, result: Any) -> int:
        """Estimate size of cached result"""
        try:
            return len(pickle.dumps(result))
        except Exception:
            return 1024  # Default estimate

    async def get_prediction(
        self, current_state: StateVector, target_state: StateVector
    ) -> Optional[Any]:
        """Get pre-computed result if available"""
        # Check cache
        cache_key = hashlib.md5(
            f"{current_state.to_tuple()}_{target_state.to_tuple()}".encode(),
            usedforsecurity=False,
        ).hexdigest()

        if cache_key in self.result_cache:
            self.stats["cache_hits"] += 1
            result, _ = self.result_cache[cache_key]
            return result

        self.stats["cache_misses"] += 1
        return None

    # Example computation functions
    def _compute_search_results(
        self, state: StateVector, predictions: List[Tuple[StateVector, float]]
    ) -> Dict[str, Any]:
        """Pre-compute search results"""
        # This would integrate with search APIs
        return {
            "query": state.metadata.get("search_query", ""),
            "suggestions": ["example1", "example2"],
            "cached_at": datetime.now().isoformat(),
        }

    def _compute_code_completion(
        self, state: StateVector, predictions: List[Tuple[StateVector, float]]
    ) -> Dict[str, Any]:
        """Pre-compute code completions"""
        return {
            "context": state.metadata.get("code_context", ""),
            "completions": ["completion1", "completion2"],
            "cached_at": datetime.now().isoformat(),
        }

    def _compute_save_locations(
        self, state: StateVector, predictions: List[Tuple[StateVector, float]]
    ) -> Dict[str, Any]:
        """Pre-compute likely save locations"""
        return {
            "file_type": state.metadata.get("file_type", "unknown"),
            "suggested_paths": ["/Users/default/Documents", "/Users/default/Desktop"],
            "recent_locations": [],
            "cached_at": datetime.now().isoformat(),
        }

    def _compute_navigation_targets(
        self, state: StateVector, predictions: List[Tuple[StateVector, float]]
    ) -> Dict[str, Any]:
        """Pre-compute navigation targets"""
        return {
            "current_location": state.app_state,
            "likely_targets": ["page1", "page2"],
            "shortcuts": [],
            "cached_at": datetime.now().isoformat(),
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Get engine statistics"""
        return {
            "predictions": self.stats,
            "accuracy": self.learning_system.get_accuracy_report(),
            "cache_info": {
                "size_mb": self.cache_size_bytes / 1024 / 1024,
                "entries": len(self.result_cache),
                "hit_rate": self.stats["cache_hits"]
                / max(1, self.stats["cache_hits"] + self.stats["cache_misses"]),
            },
            "queue_info": {
                "pending_tasks": len(self.prediction_queue.queue),
                "active_tasks": len(self.prediction_queue.active_tasks),
                "resource_usage": self.prediction_queue.resource_pool,
            },
            "matrix_info": {
                "num_states": len(self.transition_matrix.state_to_idx),
                "num_transitions": self.transition_matrix.transition_counts.nnz,
            },
        }

    def set_integration_points(
        self, temporal_engine=None, workflow_engine=None, semantic_cache=None
    ):
        """Set integration points for enhanced prediction"""
        self.temporal_engine = temporal_engine
        self.workflow_engine = workflow_engine
        self.semantic_cache = semantic_cache


# Global instance management
_predictive_engine_instance: Optional[PredictivePrecomputationEngine] = None


async def get_predictive_engine() -> PredictivePrecomputationEngine:
    """Get global predictive engine instance"""
    global _predictive_engine_instance
    if _predictive_engine_instance is None:
        _predictive_engine_instance = PredictivePrecomputationEngine()
        await _predictive_engine_instance.initialize()
    return _predictive_engine_instance


# Export main classes
__all__ = [
    "PredictivePrecomputationEngine",
    "StateVector",
    "StateType",
    "PredictionTask",
    "TransitionMatrix",
    "get_predictive_engine",
]
