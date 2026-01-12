"""
Learning State Connector v100.0
================================

Connects ContinuousLearningOrchestrator with DistributedStateManager for:
1. Persistent training job state across restarts
2. Experience aggregation coordination
3. A/B test state synchronization
4. Model performance tracking state
5. Cross-instance coordination via leader election

Architecture:
    +----------------------------+
    | ContinuousLearningOrchestrator |
    +-------------+--------------+
                  |
                  v
    +-------------+--------------+
    | LearningStateConnector      |
    |  - Experience state         |
    |  - Training job state       |
    |  - A/B test state           |
    |  - Model metrics            |
    +-------------+--------------+
                  |
                  v
    +-------------+--------------+
    | DistributedStateManager     |
    |  (Redis + Local fallback)   |
    +----------------------------+

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from backend.core.async_safety import LazyAsyncLock

# Environment configuration
LEARNING_STATE_KEY_PREFIX = os.getenv("LEARNING_STATE_PREFIX", "learning:")
EXPERIENCE_BUFFER_SIZE = int(os.getenv("LEARNING_EXPERIENCE_BUFFER", "1000"))
STATE_SYNC_INTERVAL = float(os.getenv("LEARNING_STATE_SYNC_INTERVAL", "30.0"))
ENABLE_CROSS_INSTANCE_COORD = os.getenv("LEARNING_CROSS_INSTANCE", "true").lower() == "true"

logger = logging.getLogger("LearningStateConnector")


@dataclass
class TrainingJobState:
    """Persistent state for a training job."""
    job_id: str
    model_type: str
    status: str  # pending, running, completed, failed
    experience_count: int = 0
    epochs_completed: int = 0
    epochs_total: int = 0
    loss_history: List[float] = field(default_factory=list)
    best_loss: float = float("inf")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    config: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "model_type": self.model_type,
            "status": self.status,
            "experience_count": self.experience_count,
            "epochs_completed": self.epochs_completed,
            "epochs_total": self.epochs_total,
            "loss_history": self.loss_history[-50:],  # Keep last 50 for size
            "best_loss": self.best_loss,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "config": self.config,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TrainingJobState:
        return cls(**data)


@dataclass
class ABTestState:
    """Persistent state for an A/B test."""
    test_id: str
    model_type: str
    control_version: str
    treatment_version: str
    status: str  # active, completed, cancelled
    control_requests: int = 0
    treatment_requests: int = 0
    control_successes: int = 0
    treatment_successes: int = 0
    control_latency_sum: float = 0.0
    treatment_latency_sum: float = 0.0
    traffic_split: float = 0.5
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    winner: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id,
            "model_type": self.model_type,
            "control_version": self.control_version,
            "treatment_version": self.treatment_version,
            "status": self.status,
            "control_requests": self.control_requests,
            "treatment_requests": self.treatment_requests,
            "control_successes": self.control_successes,
            "treatment_successes": self.treatment_successes,
            "control_latency_sum": self.control_latency_sum,
            "treatment_latency_sum": self.treatment_latency_sum,
            "traffic_split": self.traffic_split,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "winner": self.winner,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ABTestState:
        return cls(**data)


@dataclass
class ExperienceBufferState:
    """State for experience buffer coordination."""
    buffer_id: str
    experience_type: str
    count: int = 0
    last_flush_time: float = field(default_factory=time.time)
    total_flushed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buffer_id": self.buffer_id,
            "experience_type": self.experience_type,
            "count": self.count,
            "last_flush_time": self.last_flush_time,
            "total_flushed": self.total_flushed,
        }


@dataclass
class ConnectorMetrics:
    """Metrics for the connector."""
    jobs_saved: int = 0
    jobs_loaded: int = 0
    tests_saved: int = 0
    tests_loaded: int = 0
    state_syncs: int = 0
    sync_errors: int = 0
    experiences_tracked: int = 0


class LearningStateConnector:
    """
    Connects learning orchestrator with distributed state manager.

    Provides:
    - Training job state persistence
    - A/B test state synchronization
    - Experience buffer coordination
    - Cross-instance job coordination via leader election
    """

    def __init__(self):
        self.logger = logging.getLogger("LearningStateConnector")

        # State manager reference (lazy-loaded)
        self._state_manager = None
        self._learning_orchestrator = None

        # Local cache
        self._training_jobs: Dict[str, TrainingJobState] = {}
        self._ab_tests: Dict[str, ABTestState] = {}
        self._experience_buffers: Dict[str, ExperienceBufferState] = {}

        # State
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._is_leader = False

        # Metrics
        self._metrics = ConnectorMetrics()

        # Callbacks
        self._on_leadership_callbacks: List[Callable[[bool], None]] = []

    async def start(self) -> bool:
        """Start the connector."""
        if self._running:
            return True

        self._running = True
        self.logger.info("LearningStateConnector starting...")

        # Connect to state manager
        state_ok = await self._connect_state_manager()
        if not state_ok:
            self.logger.warning("State manager not available, using local state only")

        # Restore state from persistence
        await self._restore_state()

        # Start sync loop
        self._sync_task = asyncio.create_task(self._sync_loop())

        # Setup leader election callback if available
        if self._state_manager and ENABLE_CROSS_INSTANCE_COORD:
            await self._setup_leader_election()

        self.logger.info("LearningStateConnector ready")
        return True

    async def stop(self) -> None:
        """Stop the connector."""
        self._running = False

        # Final state sync
        await self._sync_state()

        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        self.logger.info("LearningStateConnector stopped")

    async def connect_orchestrator(self, orchestrator: Any) -> None:
        """Connect to the learning orchestrator."""
        self._learning_orchestrator = orchestrator

        # Subscribe to orchestrator events
        if hasattr(orchestrator, 'on_training_started'):
            orchestrator.on_training_started(self._on_training_started)
        if hasattr(orchestrator, 'on_training_completed'):
            orchestrator.on_training_completed(self._on_training_completed)
        if hasattr(orchestrator, 'on_ab_test_updated'):
            orchestrator.on_ab_test_updated(self._on_ab_test_updated)

        self.logger.info("Connected to learning orchestrator")

    # Training Job State

    async def save_training_job(self, job: TrainingJobState) -> bool:
        """Save training job state."""
        async with self._lock:
            job.updated_at = time.time()
            self._training_jobs[job.job_id] = job

            # Persist to state manager
            if self._state_manager:
                try:
                    from backend.core.state import StateNamespace
                    await self._state_manager.set(
                        f"{LEARNING_STATE_KEY_PREFIX}job:{job.job_id}",
                        job.to_dict(),
                        StateNamespace.LEARNING,
                        ttl=86400 * 7,  # 7 days
                    )
                except Exception as e:
                    self.logger.error(f"Failed to persist job state: {e}")
                    return False

            self._metrics.jobs_saved += 1
            return True

    async def get_training_job(self, job_id: str) -> Optional[TrainingJobState]:
        """Get training job state."""
        # Check local cache first
        if job_id in self._training_jobs:
            return self._training_jobs[job_id]

        # Try to load from state manager
        if self._state_manager:
            try:
                from backend.core.state import StateNamespace
                entry = await self._state_manager.get(
                    f"{LEARNING_STATE_KEY_PREFIX}job:{job_id}",
                    StateNamespace.LEARNING,
                )
                if entry:
                    job = TrainingJobState.from_dict(entry.value)
                    self._training_jobs[job_id] = job
                    self._metrics.jobs_loaded += 1
                    return job
            except Exception as e:
                self.logger.error(f"Failed to load job state: {e}")

        return None

    async def get_active_jobs(self) -> List[TrainingJobState]:
        """Get all active training jobs."""
        active = [
            job for job in self._training_jobs.values()
            if job.status in ("pending", "running")
        ]
        return active

    async def update_job_progress(
        self,
        job_id: str,
        epochs_completed: int,
        loss: Optional[float] = None,
    ) -> bool:
        """Update training job progress."""
        job = await self.get_training_job(job_id)
        if not job:
            return False

        job.epochs_completed = epochs_completed

        if loss is not None:
            job.loss_history.append(loss)
            if loss < job.best_loss:
                job.best_loss = loss

        return await self.save_training_job(job)

    # A/B Test State

    async def save_ab_test(self, test: ABTestState) -> bool:
        """Save A/B test state."""
        async with self._lock:
            test.updated_at = time.time()
            self._ab_tests[test.test_id] = test

            # Persist to state manager
            if self._state_manager:
                try:
                    from backend.core.state import StateNamespace
                    await self._state_manager.set(
                        f"{LEARNING_STATE_KEY_PREFIX}test:{test.test_id}",
                        test.to_dict(),
                        StateNamespace.LEARNING,
                        ttl=86400 * 30,  # 30 days
                    )
                except Exception as e:
                    self.logger.error(f"Failed to persist test state: {e}")
                    return False

            self._metrics.tests_saved += 1
            return True

    async def get_ab_test(self, test_id: str) -> Optional[ABTestState]:
        """Get A/B test state."""
        if test_id in self._ab_tests:
            return self._ab_tests[test_id]

        if self._state_manager:
            try:
                from backend.core.state import StateNamespace
                entry = await self._state_manager.get(
                    f"{LEARNING_STATE_KEY_PREFIX}test:{test_id}",
                    StateNamespace.LEARNING,
                )
                if entry:
                    test = ABTestState.from_dict(entry.value)
                    self._ab_tests[test_id] = test
                    self._metrics.tests_loaded += 1
                    return test
            except Exception as e:
                self.logger.error(f"Failed to load test state: {e}")

        return None

    async def get_active_tests(self) -> List[ABTestState]:
        """Get all active A/B tests."""
        return [test for test in self._ab_tests.values() if test.status == "active"]

    async def record_ab_request(
        self,
        test_id: str,
        variant: str,  # "control" or "treatment"
        success: bool,
        latency_ms: float,
    ) -> bool:
        """Record an A/B test request result."""
        test = await self.get_ab_test(test_id)
        if not test:
            return False

        if variant == "control":
            test.control_requests += 1
            if success:
                test.control_successes += 1
            test.control_latency_sum += latency_ms
        elif variant == "treatment":
            test.treatment_requests += 1
            if success:
                test.treatment_successes += 1
            test.treatment_latency_sum += latency_ms

        return await self.save_ab_test(test)

    # Experience Buffer Coordination

    async def track_experience_buffer(
        self,
        buffer_id: str,
        experience_type: str,
        count: int,
    ) -> None:
        """Track experience buffer state for coordination."""
        if buffer_id not in self._experience_buffers:
            self._experience_buffers[buffer_id] = ExperienceBufferState(
                buffer_id=buffer_id,
                experience_type=experience_type,
            )

        state = self._experience_buffers[buffer_id]
        state.count = count
        self._metrics.experiences_tracked += count

    async def record_buffer_flush(self, buffer_id: str, flushed_count: int) -> None:
        """Record a buffer flush event."""
        if buffer_id in self._experience_buffers:
            state = self._experience_buffers[buffer_id]
            state.count = 0
            state.last_flush_time = time.time()
            state.total_flushed += flushed_count

    # Leadership

    def is_training_leader(self) -> bool:
        """Check if this instance should run training jobs."""
        return self._is_leader or not ENABLE_CROSS_INSTANCE_COORD

    def on_leadership_change(self, callback: Callable[[bool], None]) -> None:
        """Register callback for leadership changes."""
        self._on_leadership_callbacks.append(callback)

    # Metrics

    def get_metrics(self) -> Dict[str, Any]:
        """Get connector metrics."""
        return {
            "jobs_saved": self._metrics.jobs_saved,
            "jobs_loaded": self._metrics.jobs_loaded,
            "tests_saved": self._metrics.tests_saved,
            "tests_loaded": self._metrics.tests_loaded,
            "state_syncs": self._metrics.state_syncs,
            "sync_errors": self._metrics.sync_errors,
            "experiences_tracked": self._metrics.experiences_tracked,
            "active_jobs": len([j for j in self._training_jobs.values() if j.status in ("pending", "running")]),
            "active_tests": len([t for t in self._ab_tests.values() if t.status == "active"]),
            "is_leader": self._is_leader,
            "state_manager_connected": self._state_manager is not None,
        }

    # Private methods

    async def _connect_state_manager(self) -> bool:
        """Connect to the distributed state manager."""
        try:
            from backend.core.state import get_state_manager
            self._state_manager = await get_state_manager()
            return True
        except ImportError:
            self.logger.warning("DistributedStateManager not available")
            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to state manager: {e}")
            return False

    async def _setup_leader_election(self) -> None:
        """Setup leader election for training coordination."""
        try:
            if hasattr(self._state_manager, '_leader_election') and self._state_manager._leader_election:
                def on_leadership(is_leader: bool):
                    self._is_leader = is_leader
                    for callback in self._on_leadership_callbacks:
                        try:
                            callback(is_leader)
                        except Exception as e:
                            self.logger.error(f"Leadership callback error: {e}")

                self._state_manager._leader_election.on_leadership_change(on_leadership)
                self._is_leader = self._state_manager._leader_election.is_leader()

                if self._is_leader:
                    self.logger.info("This instance is the training leader")
        except Exception as e:
            self.logger.error(f"Leader election setup failed: {e}")

    async def _restore_state(self) -> None:
        """Restore state from persistence."""
        if not self._state_manager:
            return

        try:
            from backend.core.state import StateNamespace

            # Load active training jobs
            # Note: In a full implementation, you'd query by pattern
            # For now, we rely on state manager's persistence

            self.logger.info("State restoration complete")

        except Exception as e:
            self.logger.error(f"State restoration failed: {e}")

    async def _sync_state(self) -> None:
        """Sync local state to persistent storage."""
        if not self._state_manager:
            return

        try:
            # Sync all cached jobs
            for job in self._training_jobs.values():
                await self.save_training_job(job)

            # Sync all cached tests
            for test in self._ab_tests.values():
                await self.save_ab_test(test)

            self._metrics.state_syncs += 1

        except Exception as e:
            self.logger.error(f"State sync failed: {e}")
            self._metrics.sync_errors += 1

    async def _sync_loop(self) -> None:
        """Background sync loop."""
        while self._running:
            try:
                await asyncio.sleep(STATE_SYNC_INTERVAL)

                if not self._running:
                    break

                await self._sync_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync loop error: {e}")
                self._metrics.sync_errors += 1

    # Event handlers

    async def _on_training_started(self, job_info: Dict[str, Any]) -> None:
        """Handle training started event."""
        job = TrainingJobState(
            job_id=job_info.get("job_id", ""),
            model_type=job_info.get("model_type", ""),
            status="running",
            experience_count=job_info.get("experience_count", 0),
            epochs_total=job_info.get("epochs_total", 1),
            started_at=time.time(),
            config=job_info.get("config", {}),
        )
        await self.save_training_job(job)

    async def _on_training_completed(self, job_info: Dict[str, Any]) -> None:
        """Handle training completed event."""
        job_id = job_info.get("job_id", "")
        job = await self.get_training_job(job_id)
        if job:
            job.status = job_info.get("status", "completed")
            job.completed_at = time.time()
            job.metadata.update(job_info.get("metadata", {}))
            await self.save_training_job(job)

    async def _on_ab_test_updated(self, test_info: Dict[str, Any]) -> None:
        """Handle A/B test update event."""
        test_id = test_info.get("test_id", "")
        test = await self.get_ab_test(test_id)

        if test:
            # Update from event
            test.status = test_info.get("status", test.status)
            test.winner = test_info.get("winner", test.winner)
            if test_info.get("completed"):
                test.completed_at = time.time()
            await self.save_ab_test(test)
        else:
            # Create new test
            test = ABTestState(
                test_id=test_id,
                model_type=test_info.get("model_type", ""),
                control_version=test_info.get("control_version", ""),
                treatment_version=test_info.get("treatment_version", ""),
                status=test_info.get("status", "active"),
                traffic_split=test_info.get("traffic_split", 0.5),
            )
            await self.save_ab_test(test)


# Global instance
_connector: Optional[LearningStateConnector] = None
_connector_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_learning_state_connector() -> LearningStateConnector:
    """Get the global learning state connector instance."""
    global _connector

    async with _connector_lock:
        if _connector is None:
            _connector = LearningStateConnector()
            await _connector.start()

        return _connector


async def shutdown_learning_state_connector() -> None:
    """Shutdown the global learning state connector."""
    global _connector

    if _connector:
        await _connector.stop()
        _connector = None
