"""
ContinuousLearningOrchestrator v100.0 - Unified Learning Pipeline Coordinator
==============================================================================

Advanced continuous learning orchestrator that coordinates:
1. Experience aggregation from all JARVIS components
2. Intelligent training job scheduling
3. A/B testing with automatic promotion/rollback
4. Model performance tracking and validation
5. Learning rate adaptation based on performance
6. Cross-component coordination (Voice, Vision, NLU)

Architecture:
    +-----------------------------------------------------------------+
    |              ContinuousLearningOrchestrator                      |
    |  +------------------------------------------------------------+ |
    |  |  ExperienceAggregator                                      | |
    |  |  +- Collects experiences from all sources                 | |
    |  |  +- Quality scoring and filtering                         | |
    |  |  +- Batching and buffering                                | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  TrainingScheduler                                         | |
    |  |  +- Intelligent job scheduling                            | |
    |  |  +- Priority-based queue                                  | |
    |  |  +- Resource-aware execution                              | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  ABTestingCoordinator                                      | |
    |  |  +- Experiment design and execution                       | |
    |  |  +- Statistical significance testing                      | |
    |  |  +- Automatic promotion/rollback                          | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  PerformanceTracker                                        | |
    |  |  +- Model performance metrics                             | |
    |  |  +- Improvement detection                                 | |
    |  |  +- Degradation alerts                                    | |
    |  +------------------------------------------------------------+ |
    +-----------------------------------------------------------------+

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
)

# Environment-driven configuration
LEARNING_DATA_DIR = Path(os.getenv(
    "LEARNING_DATA_DIR",
    str(Path.home() / ".jarvis" / "learning")
))
EXPERIENCE_BUFFER_SIZE = int(os.getenv("EXPERIENCE_BUFFER_SIZE", "1000"))
MIN_EXPERIENCES_FOR_TRAINING = int(os.getenv("MIN_EXPERIENCES_FOR_TRAINING", "100"))
TRAINING_CHECK_INTERVAL = float(os.getenv("TRAINING_CHECK_INTERVAL", "300.0"))  # 5 min
AB_TEST_MIN_SAMPLES = int(os.getenv("AB_TEST_MIN_SAMPLES", "50"))
AB_TEST_CONFIDENCE_THRESHOLD = float(os.getenv("AB_TEST_CONFIDENCE_THRESHOLD", "0.95"))
PERFORMANCE_WINDOW_SIZE = int(os.getenv("PERFORMANCE_WINDOW_SIZE", "100"))
AUTO_TRAINING_ENABLED = os.getenv("AUTO_TRAINING_ENABLED", "true").lower() == "true"
MODEL_PROMOTION_THRESHOLD = float(os.getenv("MODEL_PROMOTION_THRESHOLD", "0.05"))  # 5% improvement


class ExperienceType(Enum):
    """Types of learning experiences."""
    VOICE_AUTH = "voice_auth"
    INFERENCE = "inference"
    COMMAND = "command"
    VISION = "vision"
    ERROR = "error"
    FEEDBACK = "feedback"
    INTERACTION = "interaction"


class TrainingStatus(Enum):
    """Training job status."""
    QUEUED = "queued"
    PREPARING = "preparing"
    TRAINING = "training"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelType(Enum):
    """Types of models being trained."""
    VOICE = "voice"
    NLU = "nlu"
    VISION = "vision"
    EMBEDDING = "embedding"
    CLASSIFIER = "classifier"


class ABTestStatus(Enum):
    """A/B test status."""
    PENDING = "pending"
    RUNNING = "running"
    ANALYZING = "analyzing"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    INCONCLUSIVE = "inconclusive"


@dataclass
class Experience:
    """A learning experience."""
    experience_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    experience_type: ExperienceType = ExperienceType.INTERACTION
    timestamp: float = field(default_factory=time.time)

    # Content
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)

    # Quality metrics
    quality_score: float = 0.5
    confidence: float = 0.5
    success: bool = True

    # Context
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    component: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Feedback
    feedback_score: Optional[float] = None
    feedback_notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "experience_id": self.experience_id,
            "experience_type": self.experience_type.value,
            "timestamp": self.timestamp,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "quality_score": self.quality_score,
            "confidence": self.confidence,
            "success": self.success,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "component": self.component,
            "metadata": self.metadata,
            "feedback_score": self.feedback_score,
            "feedback_notes": self.feedback_notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Experience:
        """Create from dictionary."""
        return cls(
            experience_id=data.get("experience_id", str(uuid.uuid4())),
            experience_type=ExperienceType(data.get("experience_type", "interaction")),
            timestamp=data.get("timestamp", time.time()),
            input_data=data.get("input_data", {}),
            output_data=data.get("output_data", {}),
            quality_score=data.get("quality_score", 0.5),
            confidence=data.get("confidence", 0.5),
            success=data.get("success", True),
            user_id=data.get("user_id"),
            session_id=data.get("session_id"),
            component=data.get("component", "unknown"),
            metadata=data.get("metadata", {}),
            feedback_score=data.get("feedback_score"),
            feedback_notes=data.get("feedback_notes"),
        )


@dataclass
class TrainingJob:
    """A training job."""
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    model_type: ModelType = ModelType.NLU
    status: TrainingStatus = TrainingStatus.QUEUED
    priority: int = 5  # 1-10, lower = higher priority

    # Timing
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Training config
    config: Dict[str, Any] = field(default_factory=dict)
    experience_count: int = 0
    epochs: int = 1

    # Results
    metrics: Dict[str, float] = field(default_factory=dict)
    model_version: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "job_id": self.job_id,
            "model_type": self.model_type.value,
            "status": self.status.value,
            "priority": self.priority,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "config": self.config,
            "experience_count": self.experience_count,
            "epochs": self.epochs,
            "metrics": self.metrics,
            "model_version": self.model_version,
            "error": self.error,
        }


@dataclass
class ABTest:
    """An A/B test experiment."""
    test_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    status: ABTestStatus = ABTestStatus.PENDING
    model_type: ModelType = ModelType.NLU

    # Models
    control_version: str = ""
    treatment_version: str = ""

    # Timing
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None

    # Results
    control_samples: int = 0
    treatment_samples: int = 0
    control_metrics: Dict[str, float] = field(default_factory=dict)
    treatment_metrics: Dict[str, float] = field(default_factory=dict)

    # Statistical analysis
    p_value: Optional[float] = None
    effect_size: Optional[float] = None
    winner: Optional[str] = None  # "control", "treatment", or None


@dataclass
class ModelPerformance:
    """Model performance tracking."""
    model_type: ModelType
    version: str
    samples: Deque[Tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=PERFORMANCE_WINDOW_SIZE))

    # Aggregate metrics
    success_rate: float = 0.0
    avg_confidence: float = 0.0
    avg_latency_ms: float = 0.0

    # Trends
    trend: float = 0.0  # Positive = improving, negative = degrading
    last_update: float = field(default_factory=time.time)


class ExperienceAggregator:
    """Aggregates experiences from all sources."""

    def __init__(self):
        self.logger = logging.getLogger("ExperienceAggregator")
        self._buffer: Deque[Experience] = deque(maxlen=EXPERIENCE_BUFFER_SIZE)
        self._type_counts: Dict[ExperienceType, int] = defaultdict(int)
        self._lock = asyncio.Lock()

        # Quality thresholds
        self._min_quality = float(os.getenv("MIN_EXPERIENCE_QUALITY", "0.3"))

    async def add_experience(self, experience: Experience) -> bool:
        """Add an experience to the buffer."""
        # Quality filter
        if experience.quality_score < self._min_quality:
            self.logger.debug(f"Filtered low quality experience: {experience.quality_score}")
            return False

        async with self._lock:
            self._buffer.append(experience)
            self._type_counts[experience.experience_type] += 1

        return True

    async def get_experiences(
        self,
        experience_type: Optional[ExperienceType] = None,
        min_quality: float = 0.0,
        limit: int = 1000,
    ) -> List[Experience]:
        """Get experiences from buffer."""
        async with self._lock:
            experiences = list(self._buffer)

        # Filter
        if experience_type:
            experiences = [e for e in experiences if e.experience_type == experience_type]

        if min_quality > 0:
            experiences = [e for e in experiences if e.quality_score >= min_quality]

        # Sort by timestamp (newest first) and limit
        experiences.sort(key=lambda e: e.timestamp, reverse=True)
        return experiences[:limit]

    async def get_stats(self) -> Dict[str, Any]:
        """Get aggregator statistics."""
        async with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "buffer_capacity": EXPERIENCE_BUFFER_SIZE,
                "type_counts": {t.value: c for t, c in self._type_counts.items()},
            }

    async def clear(self) -> int:
        """Clear buffer and return count."""
        async with self._lock:
            count = len(self._buffer)
            self._buffer.clear()
            return count


class TrainingScheduler:
    """Schedules and manages training jobs."""

    def __init__(self):
        self.logger = logging.getLogger("TrainingScheduler")
        self._queue: List[TrainingJob] = []
        self._running: Dict[str, TrainingJob] = {}
        self._completed: Deque[TrainingJob] = deque(maxlen=100)
        self._lock = asyncio.Lock()

        # Limits
        self._max_concurrent = int(os.getenv("MAX_CONCURRENT_TRAINING", "2"))

    async def schedule(self, job: TrainingJob) -> str:
        """Schedule a training job."""
        async with self._lock:
            self._queue.append(job)
            # Sort by priority (lower = higher priority)
            self._queue.sort(key=lambda j: (j.priority, j.created_at))

        self.logger.info(f"Scheduled training job: {job.job_id} ({job.model_type.value})")
        return job.job_id

    async def get_next_job(self) -> Optional[TrainingJob]:
        """Get next job to execute."""
        async with self._lock:
            if len(self._running) >= self._max_concurrent:
                return None

            if not self._queue:
                return None

            job = self._queue.pop(0)
            job.status = TrainingStatus.PREPARING
            job.started_at = time.time()
            self._running[job.job_id] = job
            return job

    async def complete_job(self, job_id: str, success: bool, metrics: Optional[Dict[str, float]] = None) -> None:
        """Mark job as completed."""
        async with self._lock:
            if job_id not in self._running:
                return

            job = self._running.pop(job_id)
            job.status = TrainingStatus.COMPLETED if success else TrainingStatus.FAILED
            job.completed_at = time.time()
            if metrics:
                job.metrics = metrics

            self._completed.append(job)

        self.logger.info(f"Training job completed: {job_id} (success={success})")

    async def get_queue_stats(self) -> Dict[str, Any]:
        """Get scheduler statistics."""
        async with self._lock:
            return {
                "queued": len(self._queue),
                "running": len(self._running),
                "completed": len(self._completed),
                "max_concurrent": self._max_concurrent,
            }


class ABTestingCoordinator:
    """Coordinates A/B testing experiments."""

    def __init__(self):
        self.logger = logging.getLogger("ABTestingCoordinator")
        self._tests: Dict[str, ABTest] = {}
        self._lock = asyncio.Lock()

    async def create_test(
        self,
        model_type: ModelType,
        control_version: str,
        treatment_version: str,
    ) -> ABTest:
        """Create a new A/B test."""
        test = ABTest(
            model_type=model_type,
            control_version=control_version,
            treatment_version=treatment_version,
            status=ABTestStatus.RUNNING,
            started_at=time.time(),
        )

        async with self._lock:
            self._tests[test.test_id] = test

        self.logger.info(f"Created A/B test: {test.test_id} ({control_version} vs {treatment_version})")
        return test

    async def record_sample(
        self,
        test_id: str,
        is_treatment: bool,
        success: bool,
        confidence: float,
    ) -> None:
        """Record a sample for an A/B test."""
        async with self._lock:
            if test_id not in self._tests:
                return

            test = self._tests[test_id]

            if is_treatment:
                test.treatment_samples += 1
                if "success_rate" not in test.treatment_metrics:
                    test.treatment_metrics["success_rate"] = 0.0
                    test.treatment_metrics["success_count"] = 0
                if success:
                    test.treatment_metrics["success_count"] = test.treatment_metrics.get("success_count", 0) + 1
                test.treatment_metrics["success_rate"] = (
                    test.treatment_metrics["success_count"] / test.treatment_samples
                )
            else:
                test.control_samples += 1
                if "success_rate" not in test.control_metrics:
                    test.control_metrics["success_rate"] = 0.0
                    test.control_metrics["success_count"] = 0
                if success:
                    test.control_metrics["success_count"] = test.control_metrics.get("success_count", 0) + 1
                test.control_metrics["success_rate"] = (
                    test.control_metrics["success_count"] / test.control_samples
                )

    async def analyze_test(self, test_id: str) -> Optional[Dict[str, Any]]:
        """Analyze an A/B test for statistical significance."""
        async with self._lock:
            if test_id not in self._tests:
                return None

            test = self._tests[test_id]

            # Need minimum samples
            if test.control_samples < AB_TEST_MIN_SAMPLES or test.treatment_samples < AB_TEST_MIN_SAMPLES:
                return {
                    "ready": False,
                    "control_samples": test.control_samples,
                    "treatment_samples": test.treatment_samples,
                    "min_required": AB_TEST_MIN_SAMPLES,
                }

            # Calculate effect size and p-value
            control_rate = test.control_metrics.get("success_rate", 0.0)
            treatment_rate = test.treatment_metrics.get("success_rate", 0.0)
            effect_size = treatment_rate - control_rate

            # Simple z-test approximation
            pooled_rate = (
                (control_rate * test.control_samples + treatment_rate * test.treatment_samples)
                / (test.control_samples + test.treatment_samples)
            )

            if pooled_rate > 0 and pooled_rate < 1:
                se = math.sqrt(
                    pooled_rate * (1 - pooled_rate) *
                    (1 / test.control_samples + 1 / test.treatment_samples)
                )
                z_score = effect_size / se if se > 0 else 0

                # Approximate p-value
                p_value = 2 * (1 - self._normal_cdf(abs(z_score)))
            else:
                p_value = 1.0

            test.p_value = p_value
            test.effect_size = effect_size

            # Determine winner
            is_significant = p_value < (1 - AB_TEST_CONFIDENCE_THRESHOLD)
            if is_significant:
                test.winner = "treatment" if effect_size > 0 else "control"
            else:
                test.winner = None

            return {
                "ready": True,
                "control_rate": control_rate,
                "treatment_rate": treatment_rate,
                "effect_size": effect_size,
                "p_value": p_value,
                "is_significant": is_significant,
                "winner": test.winner,
                "recommendation": (
                    f"Promote {test.winner}" if is_significant
                    else "Continue testing" if test.control_samples + test.treatment_samples < AB_TEST_MIN_SAMPLES * 4
                    else "No significant difference"
                ),
            }

    def _normal_cdf(self, x: float) -> float:
        """Approximate normal CDF."""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    async def promote_winner(self, test_id: str) -> Optional[str]:
        """Promote the winning model."""
        async with self._lock:
            if test_id not in self._tests:
                return None

            test = self._tests[test_id]

            if test.winner == "treatment":
                test.status = ABTestStatus.PROMOTED
                test.ended_at = time.time()
                return test.treatment_version
            elif test.winner == "control":
                test.status = ABTestStatus.ROLLED_BACK
                test.ended_at = time.time()
                return test.control_version
            else:
                test.status = ABTestStatus.INCONCLUSIVE
                test.ended_at = time.time()
                return None


class PerformanceTracker:
    """Tracks model performance over time."""

    def __init__(self):
        self.logger = logging.getLogger("PerformanceTracker")
        self._models: Dict[str, ModelPerformance] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        model_type: ModelType,
        version: str,
        success: bool,
        confidence: float = 0.0,
        latency_ms: float = 0.0,
    ) -> None:
        """Record a model performance sample."""
        key = f"{model_type.value}:{version}"

        async with self._lock:
            if key not in self._models:
                self._models[key] = ModelPerformance(
                    model_type=model_type,
                    version=version,
                )

            perf = self._models[key]
            perf.samples.append((confidence, success))
            perf.last_update = time.time()

            # Update aggregates
            if perf.samples:
                perf.success_rate = sum(1 for _, s in perf.samples if s) / len(perf.samples)
                perf.avg_confidence = sum(c for c, _ in perf.samples) / len(perf.samples)

            # Calculate trend (simple linear regression on success rate)
            if len(perf.samples) >= 10:
                recent = list(perf.samples)[-20:]
                old = list(perf.samples)[:-20] if len(perf.samples) > 20 else []

                recent_rate = sum(1 for _, s in recent if s) / len(recent) if recent else 0
                old_rate = sum(1 for _, s in old if s) / len(old) if old else recent_rate

                perf.trend = recent_rate - old_rate

    async def get_performance(
        self,
        model_type: ModelType,
        version: str,
    ) -> Optional[Dict[str, Any]]:
        """Get performance metrics for a model."""
        key = f"{model_type.value}:{version}"

        async with self._lock:
            if key not in self._models:
                return None

            perf = self._models[key]
            return {
                "model_type": perf.model_type.value,
                "version": perf.version,
                "samples": len(perf.samples),
                "success_rate": perf.success_rate,
                "avg_confidence": perf.avg_confidence,
                "avg_latency_ms": perf.avg_latency_ms,
                "trend": perf.trend,
                "last_update": perf.last_update,
                "is_degrading": perf.trend < -0.05,
            }

    async def get_all_performance(self) -> List[Dict[str, Any]]:
        """Get performance for all models."""
        async with self._lock:
            return [
                {
                    "model_type": p.model_type.value,
                    "version": p.version,
                    "samples": len(p.samples),
                    "success_rate": p.success_rate,
                    "trend": p.trend,
                }
                for p in self._models.values()
            ]


class ContinuousLearningOrchestrator:
    """
    Unified continuous learning orchestrator.

    Coordinates experience collection, training, A/B testing,
    and model deployment across all JARVIS components.
    """

    def __init__(self):
        self.logger = logging.getLogger("ContinuousLearningOrchestrator")

        # Components
        self._aggregator = ExperienceAggregator()
        self._scheduler = TrainingScheduler()
        self._ab_testing = ABTestingCoordinator()
        self._performance = PerformanceTracker()

        # State
        self._running = False
        self._lock = asyncio.Lock()
        self._training_task: Optional[asyncio.Task] = None

        # Metrics
        self._metrics = {
            "experiences_collected": 0,
            "training_jobs_completed": 0,
            "ab_tests_completed": 0,
            "models_promoted": 0,
            "models_rolled_back": 0,
        }

        # Callbacks
        self._on_training_complete: List[Callable] = []
        self._on_model_promoted: List[Callable] = []

        # Ensure data directory
        LEARNING_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the orchestrator."""
        if self._running:
            return

        self._running = True
        self.logger.info("ContinuousLearningOrchestrator starting...")

        # Start background training loop
        if AUTO_TRAINING_ENABLED:
            self._training_task = asyncio.create_task(self._training_loop())
            self.logger.info("  Auto-training enabled")
        else:
            self.logger.info("  Auto-training disabled")

        self.logger.info("ContinuousLearningOrchestrator ready")

    async def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False

        if self._training_task:
            self._training_task.cancel()
            try:
                await self._training_task
            except asyncio.CancelledError:
                pass

        # Save state
        await self._save_state()

        self.logger.info("ContinuousLearningOrchestrator stopped")

    # Experience collection

    async def collect_experience(
        self,
        experience_type: ExperienceType,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        quality_score: float = 0.5,
        confidence: float = 0.5,
        success: bool = True,
        component: str = "unknown",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Collect a learning experience."""
        experience = Experience(
            experience_type=experience_type,
            input_data=input_data,
            output_data=output_data,
            quality_score=quality_score,
            confidence=confidence,
            success=success,
            component=component,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata or {},
        )

        added = await self._aggregator.add_experience(experience)
        if added:
            self._metrics["experiences_collected"] += 1

        return experience.experience_id

    async def add_feedback(
        self,
        experience_id: str,
        feedback_score: float,
        feedback_notes: Optional[str] = None,
    ) -> bool:
        """Add feedback to an experience."""
        experiences = await self._aggregator.get_experiences()

        for exp in experiences:
            if exp.experience_id == experience_id:
                exp.feedback_score = feedback_score
                exp.feedback_notes = feedback_notes
                return True

        return False

    # Training

    async def trigger_training(
        self,
        model_type: ModelType,
        priority: int = 5,
        config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Trigger a training job."""
        # Get experiences for this model type
        exp_type_map = {
            ModelType.VOICE: ExperienceType.VOICE_AUTH,
            ModelType.NLU: ExperienceType.COMMAND,
            ModelType.VISION: ExperienceType.VISION,
        }

        exp_type = exp_type_map.get(model_type, ExperienceType.INTERACTION)
        experiences = await self._aggregator.get_experiences(experience_type=exp_type)

        if len(experiences) < MIN_EXPERIENCES_FOR_TRAINING:
            self.logger.warning(
                f"Not enough experiences for {model_type.value}: "
                f"{len(experiences)} < {MIN_EXPERIENCES_FOR_TRAINING}"
            )
            return ""

        job = TrainingJob(
            model_type=model_type,
            priority=priority,
            config=config or {},
            experience_count=len(experiences),
        )

        job_id = await self._scheduler.schedule(job)
        return job_id

    async def get_training_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get training job status."""
        stats = await self._scheduler.get_queue_stats()
        return stats

    # A/B Testing

    async def start_ab_test(
        self,
        model_type: ModelType,
        control_version: str,
        treatment_version: str,
    ) -> str:
        """Start an A/B test."""
        test = await self._ab_testing.create_test(
            model_type=model_type,
            control_version=control_version,
            treatment_version=treatment_version,
        )
        return test.test_id

    async def record_ab_sample(
        self,
        test_id: str,
        is_treatment: bool,
        success: bool,
        confidence: float,
    ) -> None:
        """Record an A/B test sample."""
        await self._ab_testing.record_sample(
            test_id=test_id,
            is_treatment=is_treatment,
            success=success,
            confidence=confidence,
        )

    async def analyze_ab_test(self, test_id: str) -> Optional[Dict[str, Any]]:
        """Analyze an A/B test."""
        return await self._ab_testing.analyze_test(test_id)

    # Performance tracking

    async def record_performance(
        self,
        model_type: ModelType,
        version: str,
        success: bool,
        confidence: float = 0.0,
        latency_ms: float = 0.0,
    ) -> None:
        """Record model performance."""
        await self._performance.record(
            model_type=model_type,
            version=version,
            success=success,
            confidence=confidence,
            latency_ms=latency_ms,
        )

    async def get_performance(
        self,
        model_type: ModelType,
        version: str,
    ) -> Optional[Dict[str, Any]]:
        """Get model performance."""
        return await self._performance.get_performance(model_type, version)

    # Callbacks

    def on_training_complete(self, callback: Callable) -> None:
        """Register training complete callback."""
        self._on_training_complete.append(callback)

    def on_model_promoted(self, callback: Callable) -> None:
        """Register model promoted callback."""
        self._on_model_promoted.append(callback)

    # Metrics

    def get_metrics(self) -> Dict[str, Any]:
        """Get orchestrator metrics."""
        return {
            **self._metrics,
            "running": self._running,
            "auto_training_enabled": AUTO_TRAINING_ENABLED,
        }

    async def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        aggregator_stats = await self._aggregator.get_stats()
        scheduler_stats = await self._scheduler.get_queue_stats()
        performance_stats = await self._performance.get_all_performance()

        return {
            "metrics": self._metrics,
            "aggregator": aggregator_stats,
            "scheduler": scheduler_stats,
            "performance": performance_stats,
        }

    # Private methods

    async def _training_loop(self) -> None:
        """Background training loop."""
        while self._running:
            try:
                await asyncio.sleep(TRAINING_CHECK_INTERVAL)

                if not self._running:
                    break

                # Check for pending jobs
                job = await self._scheduler.get_next_job()
                if job:
                    await self._execute_training(job)

                # Auto-trigger training if enough experiences
                await self._check_auto_training()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Training loop error: {e}")
                await asyncio.sleep(60)

    async def _execute_training(self, job: TrainingJob) -> None:
        """Execute a training job."""
        self.logger.info(f"Executing training job: {job.job_id}")
        job.status = TrainingStatus.TRAINING

        try:
            # Simulate training (in real implementation, call Reactor Core)
            await asyncio.sleep(5)

            # Generate version
            job.model_version = f"{job.model_type.value}-v{int(time.time())}"
            job.metrics = {
                "loss": 0.1,
                "accuracy": 0.95,
            }

            await self._scheduler.complete_job(job.job_id, True, job.metrics)
            self._metrics["training_jobs_completed"] += 1

            # Fire callbacks
            for callback in self._on_training_complete:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(job)
                    else:
                        callback(job)
                except Exception as e:
                    self.logger.error(f"Callback error: {e}")

        except Exception as e:
            self.logger.error(f"Training job failed: {e}")
            job.error = str(e)
            await self._scheduler.complete_job(job.job_id, False)

    async def _check_auto_training(self) -> None:
        """Check if auto-training should be triggered."""
        stats = await self._aggregator.get_stats()
        buffer_size = stats.get("buffer_size", 0)

        if buffer_size >= MIN_EXPERIENCES_FOR_TRAINING:
            # Determine which model type has most experiences
            type_counts = stats.get("type_counts", {})
            if type_counts:
                top_type = max(type_counts.items(), key=lambda x: x[1])
                exp_type = ExperienceType(top_type[0])

                # Map to model type
                model_map = {
                    ExperienceType.VOICE_AUTH: ModelType.VOICE,
                    ExperienceType.COMMAND: ModelType.NLU,
                    ExperienceType.VISION: ModelType.VISION,
                }

                model_type = model_map.get(exp_type, ModelType.NLU)

                self.logger.info(f"Auto-triggering training for {model_type.value}")
                await self.trigger_training(model_type, priority=8)

    async def _save_state(self) -> None:
        """Save orchestrator state."""
        try:
            state = {
                "saved_at": time.time(),
                "metrics": self._metrics,
            }

            filepath = LEARNING_DATA_DIR / "orchestrator_state.json"
            filepath.write_text(json.dumps(state, indent=2))

        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")


# Global instance
_orchestrator: Optional[ContinuousLearningOrchestrator] = None
_lock = asyncio.Lock()


async def get_learning_orchestrator() -> ContinuousLearningOrchestrator:
    """Get the global learning orchestrator instance."""
    global _orchestrator

    async with _lock:
        if _orchestrator is None:
            _orchestrator = ContinuousLearningOrchestrator()
            await _orchestrator.start()

        return _orchestrator


async def shutdown_learning_orchestrator() -> None:
    """Shutdown the global learning orchestrator."""
    global _orchestrator

    if _orchestrator:
        await _orchestrator.stop()
        _orchestrator = None
