"""
Trinity Training Pipeline v2.7
==============================

Complete training pipeline integration across Trinity:
- JARVIS (Body) → Experience capture, feedback collection
- Reactor Core (Nerves) → Training orchestration
- JARVIS Prime (Mind) → Model deployment, hot-swap

Pipeline Flow:
    ┌──────────────────────────────────────────────────────────────────┐
    │                    TRINITY TRAINING PIPELINE                      │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  ┌─────────────┐                                                 │
    │  │   JARVIS    │  1. Capture experiences                        │
    │  │   (Body)    │  2. User feedback                              │
    │  │             │  3. Error patterns                              │
    │  └──────┬──────┘                                                 │
    │         │                                                        │
    │         │ experience.stream                                      │
    │         ▼                                                        │
    │  ┌─────────────┐                                                 │
    │  │   Reactor   │  1. Aggregate experiences                      │
    │  │   Core      │  2. Train models (LoRA/fine-tune)              │
    │  │  (Nerves)   │  3. Evaluate performance                       │
    │  └──────┬──────┘                                                 │
    │         │                                                        │
    │         │ model.trained                                          │
    │         ▼                                                        │
    │  ┌─────────────┐                                                 │
    │  │   JARVIS    │  1. Receive new model                          │
    │  │   Prime     │  2. Hot-swap (zero downtime)                   │
    │  │   (Mind)    │  3. A/B test vs previous                       │
    │  └──────┬──────┘                                                 │
    │         │                                                        │
    │         │ model.deployed                                         │
    │         ▼                                                        │
    │  ┌─────────────┐                                                 │
    │  │   JARVIS    │  1. Use new model                              │
    │  │   (Body)    │  2. Capture performance                        │
    │  │             │  3. Feedback loop continues                    │
    │  └─────────────┘                                                 │
    │                                                                   │
    └──────────────────────────────────────────────────────────────────┘

Features:
- Automatic experience streaming
- Training job orchestration
- Model versioning and rollback
- Hot-swap with zero downtime
- A/B testing
- Feedback loop integration
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


class PipelineConfig:
    """Training pipeline configuration."""

    # Experience settings
    EXPERIENCE_BUFFER_SIZE = _env_int("TRAINING_EXPERIENCE_BUFFER", 1000)
    EXPERIENCE_FLUSH_INTERVAL = _env_float("TRAINING_FLUSH_INTERVAL", 60.0)
    MIN_EXPERIENCES_FOR_TRAINING = _env_int("TRAINING_MIN_EXPERIENCES", 100)

    # Training settings
    TRAINING_TRIGGER_INTERVAL = _env_float("TRAINING_TRIGGER_INTERVAL", 3600.0)  # 1 hour
    AUTO_TRAINING_ENABLED = _env_bool("TRAINING_AUTO_ENABLED", True)

    # Model deployment settings
    MODEL_A_B_TEST_ENABLED = _env_bool("MODEL_AB_TEST_ENABLED", True)
    MODEL_A_B_TEST_PERCENTAGE = _env_float("MODEL_AB_TEST_PERCENTAGE", 10.0)
    MODEL_ROLLBACK_THRESHOLD = _env_float("MODEL_ROLLBACK_THRESHOLD", 0.9)

    # Reactor Core settings
    REACTOR_CORE_URL = _env_str("REACTOR_CORE_API_URL", "http://localhost:8090")
    REACTOR_CORE_TIMEOUT = _env_float("REACTOR_CORE_TIMEOUT", 30.0)

    # Prime settings
    PRIME_URL = _env_str("JARVIS_PRIME_URL", "http://localhost:8000")
    PRIME_TIMEOUT = _env_float("JARVIS_PRIME_TIMEOUT", 30.0)

    # Storage
    EXPERIENCE_STORE_PATH = _env_str(
        "TRAINING_EXPERIENCE_STORE",
        str(Path.home() / ".jarvis" / "training" / "experiences")
    )
    MODEL_STORE_PATH = _env_str(
        "TRAINING_MODEL_STORE",
        str(Path.home() / ".jarvis" / "training" / "models")
    )


# =============================================================================
# Enums and Types
# =============================================================================

class ExperienceType(Enum):
    """Types of training experiences."""
    INFERENCE = "inference"          # Model inference request/response
    VOICE_AUTH = "voice_auth"        # Voice authentication attempt
    COMMAND = "command"              # User command execution
    ERROR = "error"                  # Error occurrence
    FEEDBACK = "feedback"            # Explicit user feedback
    CORRECTION = "correction"        # User correction
    CONVERSATION = "conversation"    # Conversation exchange
    WORKFLOW = "workflow"            # Workflow execution


class TrainingJobStatus(Enum):
    """Training job status."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelStatus(Enum):
    """Model deployment status."""
    TRAINING = "training"
    READY = "ready"
    DEPLOYING = "deploying"
    ACTIVE = "active"
    A_B_TESTING = "ab_testing"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


class FeedbackType(Enum):
    """User feedback types."""
    POSITIVE = "positive"      # Thumbs up, correct response
    NEGATIVE = "negative"      # Thumbs down, incorrect
    CORRECTION = "correction"  # User provided correct answer
    SKIP = "skip"              # User skipped/ignored


@dataclass
class Experience:
    """A training experience captured from JARVIS."""
    experience_id: str = ""
    experience_type: ExperienceType = ExperienceType.INFERENCE
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    feedback: Optional[FeedbackType] = None
    feedback_data: Optional[Dict[str, Any]] = None
    quality_score: float = 0.5
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if not self.experience_id:
            content = f"{self.experience_type.value}:{self.timestamp.isoformat()}:{json.dumps(self.input_data)}"
            self.experience_id = hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "experience_type": self.experience_type.value,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "metadata": self.metadata,
            "feedback": self.feedback.value if self.feedback else None,
            "feedback_data": self.feedback_data,
            "quality_score": self.quality_score,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Experience":
        return cls(
            experience_id=data["experience_id"],
            experience_type=ExperienceType(data["experience_type"]),
            input_data=data.get("input_data", {}),
            output_data=data.get("output_data", {}),
            metadata=data.get("metadata", {}),
            feedback=FeedbackType(data["feedback"]) if data.get("feedback") else None,
            feedback_data=data.get("feedback_data"),
            quality_score=data.get("quality_score", 0.5),
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


@dataclass
class TrainingJob:
    """A training job for Reactor Core."""
    job_id: str = ""
    model_name: str = ""
    model_type: str = "lora"  # lora, full, qlora
    experience_ids: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    status: TrainingJobStatus = TrainingJobStatus.PENDING
    progress: float = 0.0
    metrics: Dict[str, float] = field(default_factory=dict)
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    output_model_path: Optional[str] = None

    def __post_init__(self):
        if not self.job_id:
            content = f"{self.model_name}:{self.created_at.isoformat()}"
            self.job_id = hashlib.sha256(content.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "model_name": self.model_name,
            "model_type": self.model_type,
            "experience_ids": self.experience_ids,
            "config": self.config,
            "status": self.status.value,
            "progress": self.progress,
            "metrics": self.metrics,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "output_model_path": self.output_model_path,
        }


@dataclass
class ModelVersion:
    """A deployed model version."""
    version_id: str = ""
    model_name: str = ""
    training_job_id: str = ""
    model_path: str = ""
    status: ModelStatus = ModelStatus.READY
    performance_metrics: Dict[str, float] = field(default_factory=dict)
    deployment_config: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    deployed_at: Optional[datetime] = None
    rollback_version_id: Optional[str] = None

    def __post_init__(self):
        if not self.version_id:
            content = f"{self.model_name}:{self.created_at.isoformat()}"
            self.version_id = hashlib.sha256(content.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "model_name": self.model_name,
            "training_job_id": self.training_job_id,
            "model_path": self.model_path,
            "status": self.status.value,
            "performance_metrics": self.performance_metrics,
            "deployment_config": self.deployment_config,
            "created_at": self.created_at.isoformat(),
            "deployed_at": self.deployed_at.isoformat() if self.deployed_at else None,
            "rollback_version_id": self.rollback_version_id,
        }


@dataclass
class PipelineStats:
    """Training pipeline statistics."""
    experiences_captured: int = 0
    experiences_pending: int = 0
    training_jobs_total: int = 0
    training_jobs_completed: int = 0
    models_deployed: int = 0
    current_model_version: Optional[str] = None
    last_training_at: Optional[datetime] = None
    last_deployment_at: Optional[datetime] = None
    feedback_positive: int = 0
    feedback_negative: int = 0


# =============================================================================
# Training Pipeline Manager
# =============================================================================

class TrinityTrainingPipeline:
    """
    Complete training pipeline for Trinity ecosystem.

    Handles:
    - Experience capture from JARVIS
    - Training job orchestration with Reactor Core
    - Model deployment to JARVIS Prime
    - Feedback loop and continuous learning
    """

    def __init__(self):
        self._running = False

        # Experience buffer
        self._experience_buffer: List[Experience] = []
        self._buffer_lock = asyncio.Lock()

        # Job tracking
        self._active_jobs: Dict[str, TrainingJob] = {}
        self._job_history: List[TrainingJob] = []

        # Model versions
        self._model_versions: Dict[str, ModelVersion] = {}
        self._active_model: Optional[str] = None
        self._ab_test_model: Optional[str] = None

        # Clients
        self._reactor_client: Optional[Any] = None
        self._prime_client: Optional[Any] = None
        self._event_bus: Optional[Any] = None

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._training_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = PipelineStats()

        # Ensure directories
        Path(PipelineConfig.EXPERIENCE_STORE_PATH).mkdir(parents=True, exist_ok=True)
        Path(PipelineConfig.MODEL_STORE_PATH).mkdir(parents=True, exist_ok=True)

        logger.info("[TrainingPipeline] Initialized")

    @classmethod
    async def create(cls) -> "TrinityTrainingPipeline":
        """Create and initialize the pipeline."""
        pipeline = cls()
        await pipeline.initialize()
        return pipeline

    async def initialize(self) -> None:
        """Initialize pipeline components."""
        # Initialize clients
        await self._init_reactor_client()
        await self._init_prime_client()
        await self._connect_event_bus()

        self._running = True

        # Start background tasks
        self._flush_task = asyncio.create_task(self._flush_loop())

        if PipelineConfig.AUTO_TRAINING_ENABLED:
            self._training_task = asyncio.create_task(self._auto_training_loop())

        self._monitor_task = asyncio.create_task(self._monitor_loop())

        logger.info("[TrainingPipeline] Initialization complete")

    async def _init_reactor_client(self) -> None:
        """Initialize Reactor Core client."""
        try:
            from backend.clients.reactor_core_client import get_reactor_core_client
            self._reactor_client = await get_reactor_core_client()
            logger.info("[TrainingPipeline] Reactor Core client connected")
        except ImportError:
            logger.warning("[TrainingPipeline] Reactor Core client not available")
        except Exception as e:
            logger.warning(f"[TrainingPipeline] Reactor Core connection failed: {e}")

    async def _init_prime_client(self) -> None:
        """Initialize JARVIS Prime client."""
        try:
            from backend.clients.jarvis_prime_client import get_jarvis_prime_client
            self._prime_client = await get_jarvis_prime_client()
            logger.info("[TrainingPipeline] JARVIS Prime client connected")
        except ImportError:
            logger.warning("[TrainingPipeline] JARVIS Prime client not available")
        except Exception as e:
            logger.warning(f"[TrainingPipeline] Prime connection failed: {e}")

    async def _connect_event_bus(self) -> None:
        """Connect to Trinity event bus."""
        try:
            from backend.core.trinity_event_bus import (
                get_trinity_event_bus,
                TrinityEvent,
                EventPriority,
                RepoType,
            )

            self._event_bus = await get_trinity_event_bus(RepoType.JARVIS)

            # Subscribe to training events
            await self._event_bus.subscribe("training.*", self._handle_training_event)
            await self._event_bus.subscribe("model.*", self._handle_model_event)

            logger.info("[TrainingPipeline] Connected to event bus")
        except ImportError:
            logger.warning("[TrainingPipeline] Event bus not available")

    async def capture_experience(
        self,
        experience_type: ExperienceType,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        quality_score: float = 0.5,
    ) -> str:
        """
        Capture a training experience.

        Args:
            experience_type: Type of experience
            input_data: Input to the system
            output_data: System output
            metadata: Additional context
            quality_score: Initial quality estimate (0-1)

        Returns:
            Experience ID
        """
        experience = Experience(
            experience_type=experience_type,
            input_data=input_data,
            output_data=output_data,
            metadata=metadata or {},
            quality_score=quality_score,
        )

        async with self._buffer_lock:
            self._experience_buffer.append(experience)
            self._stats.experiences_captured += 1
            self._stats.experiences_pending = len(self._experience_buffer)

        logger.debug(f"[TrainingPipeline] Captured experience {experience.experience_id}")

        # Publish event
        if self._event_bus:
            from backend.core.trinity_event_bus import TrinityEvent, EventPriority
            await self._event_bus.publish(TrinityEvent(
                topic="training.experience_captured",
                payload={
                    "experience_id": experience.experience_id,
                    "experience_type": experience_type.value,
                },
                priority=EventPriority.LOW,
            ))

        return experience.experience_id

    async def add_feedback(
        self,
        experience_id: str,
        feedback: FeedbackType,
        feedback_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Add feedback to an experience.

        Args:
            experience_id: Experience to update
            feedback: Feedback type
            feedback_data: Additional feedback data (e.g., correct answer)

        Returns:
            True if feedback was added
        """
        async with self._buffer_lock:
            for exp in self._experience_buffer:
                if exp.experience_id == experience_id:
                    exp.feedback = feedback
                    exp.feedback_data = feedback_data

                    # Adjust quality score based on feedback
                    if feedback == FeedbackType.POSITIVE:
                        exp.quality_score = min(1.0, exp.quality_score + 0.3)
                        self._stats.feedback_positive += 1
                    elif feedback == FeedbackType.NEGATIVE:
                        exp.quality_score = max(0.0, exp.quality_score - 0.3)
                        self._stats.feedback_negative += 1
                    elif feedback == FeedbackType.CORRECTION:
                        exp.quality_score = 1.0  # Corrections are valuable
                        self._stats.feedback_positive += 1

                    logger.debug(f"[TrainingPipeline] Added {feedback.value} feedback to {experience_id}")
                    return True

        return False

    async def trigger_training(
        self,
        model_name: str = "jarvis-custom",
        model_type: str = "lora",
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Trigger a training job.

        Args:
            model_name: Name for the trained model
            model_type: Training type (lora, full, qlora)
            config: Training configuration

        Returns:
            Training job ID, or None if failed
        """
        # Flush experiences first
        experience_ids = await self._flush_experiences()

        if len(experience_ids) < PipelineConfig.MIN_EXPERIENCES_FOR_TRAINING:
            logger.warning(
                f"[TrainingPipeline] Not enough experiences ({len(experience_ids)}) "
                f"for training (min: {PipelineConfig.MIN_EXPERIENCES_FOR_TRAINING})"
            )
            return None

        # Create training job
        job = TrainingJob(
            model_name=model_name,
            model_type=model_type,
            experience_ids=experience_ids,
            config=config or {
                "learning_rate": 2e-5,
                "epochs": 3,
                "batch_size": 8,
                "lora_rank": 16,
                "lora_alpha": 32,
            },
        )

        # Submit to Reactor Core
        if self._reactor_client:
            try:
                result = await self._reactor_client.submit_training_job(
                    job_id=job.job_id,
                    model_name=job.model_name,
                    model_type=job.model_type,
                    experiences=job.experience_ids,
                    config=job.config,
                )

                if result.get("success"):
                    job.status = TrainingJobStatus.QUEUED
                    self._active_jobs[job.job_id] = job
                    self._stats.training_jobs_total += 1
                    self._stats.last_training_at = datetime.now()

                    # Publish event
                    if self._event_bus:
                        from backend.core.trinity_event_bus import TrinityEvent, EventPriority
                        await self._event_bus.publish(TrinityEvent(
                            topic="training.job_submitted",
                            payload=job.to_dict(),
                            priority=EventPriority.NORMAL,
                        ))

                    logger.info(f"[TrainingPipeline] Submitted training job {job.job_id}")
                    return job.job_id
                else:
                    logger.warning(f"[TrainingPipeline] Job submission failed: {result.get('error')}")
                    return None

            except Exception as e:
                logger.exception(f"[TrainingPipeline] Training submission error: {e}")
                return None
        else:
            logger.warning("[TrainingPipeline] Reactor Core not available")
            return None

    async def deploy_model(
        self,
        version_id: str,
        hot_swap: bool = True,
        ab_test: bool = False,
    ) -> bool:
        """
        Deploy a model version to JARVIS Prime.

        Args:
            version_id: Model version to deploy
            hot_swap: Use hot-swap for zero downtime
            ab_test: Enable A/B testing with current model

        Returns:
            True if deployment successful
        """
        version = self._model_versions.get(version_id)
        if not version:
            logger.warning(f"[TrainingPipeline] Model version {version_id} not found")
            return False

        if not self._prime_client:
            logger.warning("[TrainingPipeline] JARVIS Prime not available")
            return False

        try:
            # Prepare deployment
            version.status = ModelStatus.DEPLOYING
            version.deployment_config = {
                "hot_swap": hot_swap,
                "ab_test": ab_test,
                "ab_percentage": PipelineConfig.MODEL_A_B_TEST_PERCENTAGE if ab_test else 0,
            }

            # Deploy to Prime
            result = await self._prime_client.deploy_model(
                model_path=version.model_path,
                model_name=version.model_name,
                hot_swap=hot_swap,
            )

            if result.get("success"):
                version.status = ModelStatus.A_B_TESTING if ab_test else ModelStatus.ACTIVE
                version.deployed_at = datetime.now()
                version.rollback_version_id = self._active_model

                if ab_test:
                    self._ab_test_model = version_id
                else:
                    # Deprecate previous active model
                    if self._active_model and self._active_model in self._model_versions:
                        self._model_versions[self._active_model].status = ModelStatus.DEPRECATED

                    self._active_model = version_id

                self._stats.models_deployed += 1
                self._stats.current_model_version = version_id
                self._stats.last_deployment_at = datetime.now()

                # Publish event
                if self._event_bus:
                    from backend.core.trinity_event_bus import TrinityEvent, EventPriority
                    await self._event_bus.publish(TrinityEvent(
                        topic="model.deployed",
                        payload=version.to_dict(),
                        priority=EventPriority.HIGH,
                    ))

                logger.info(f"[TrainingPipeline] Deployed model version {version_id}")
                return True

            else:
                version.status = ModelStatus.READY
                logger.warning(f"[TrainingPipeline] Deployment failed: {result.get('error')}")
                return False

        except Exception as e:
            logger.exception(f"[TrainingPipeline] Deployment error: {e}")
            version.status = ModelStatus.READY
            return False

    async def rollback_model(self, to_version_id: Optional[str] = None) -> bool:
        """
        Rollback to a previous model version.

        Args:
            to_version_id: Version to rollback to (or previous if None)

        Returns:
            True if rollback successful
        """
        if not self._active_model:
            logger.warning("[TrainingPipeline] No active model to rollback from")
            return False

        current = self._model_versions.get(self._active_model)
        if not current:
            return False

        rollback_id = to_version_id or current.rollback_version_id
        if not rollback_id:
            logger.warning("[TrainingPipeline] No rollback version available")
            return False

        rollback = self._model_versions.get(rollback_id)
        if not rollback:
            logger.warning(f"[TrainingPipeline] Rollback version {rollback_id} not found")
            return False

        # Deploy rollback version
        success = await self.deploy_model(rollback_id, hot_swap=True, ab_test=False)

        if success:
            current.status = ModelStatus.ROLLED_BACK
            logger.info(f"[TrainingPipeline] Rolled back to {rollback_id}")

        return success

    async def _flush_experiences(self) -> List[str]:
        """Flush experience buffer to storage."""
        async with self._buffer_lock:
            if not self._experience_buffer:
                return []

            experiences = self._experience_buffer.copy()
            self._experience_buffer.clear()
            self._stats.experiences_pending = 0

        # Save to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = Path(PipelineConfig.EXPERIENCE_STORE_PATH) / f"experiences_{timestamp}.jsonl"

        try:
            import aiofiles
            async with aiofiles.open(filepath, "w") as f:
                for exp in experiences:
                    line = json.dumps(exp.to_dict(), default=str) + "\n"
                    await f.write(line)

            # Notify Reactor Core
            if self._reactor_client:
                await self._reactor_client.notify_experiences(
                    filepath=str(filepath),
                    count=len(experiences),
                )

            logger.info(f"[TrainingPipeline] Flushed {len(experiences)} experiences")
            return [exp.experience_id for exp in experiences]

        except Exception as e:
            logger.exception(f"[TrainingPipeline] Flush error: {e}")
            # Re-add to buffer on failure
            async with self._buffer_lock:
                self._experience_buffer.extend(experiences)
                self._stats.experiences_pending = len(self._experience_buffer)
            return []

    async def _flush_loop(self) -> None:
        """Background flush loop."""
        while self._running:
            try:
                await asyncio.sleep(PipelineConfig.EXPERIENCE_FLUSH_INTERVAL)

                if len(self._experience_buffer) >= PipelineConfig.EXPERIENCE_BUFFER_SIZE // 2:
                    await self._flush_experiences()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[TrainingPipeline] Flush loop error: {e}")

    async def _auto_training_loop(self) -> None:
        """Background auto-training loop."""
        while self._running:
            try:
                await asyncio.sleep(PipelineConfig.TRAINING_TRIGGER_INTERVAL)

                # Check if enough experiences accumulated
                async with self._buffer_lock:
                    pending = len(self._experience_buffer)

                if pending >= PipelineConfig.MIN_EXPERIENCES_FOR_TRAINING:
                    logger.info(f"[TrainingPipeline] Auto-triggering training with {pending} experiences")
                    await self.trigger_training()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[TrainingPipeline] Auto-training error: {e}")

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(30.0)

                # Check active job status
                for job_id, job in list(self._active_jobs.items()):
                    if job.status in (TrainingJobStatus.RUNNING, TrainingJobStatus.QUEUED):
                        await self._check_job_status(job)

                # Check A/B test results
                if self._ab_test_model:
                    await self._evaluate_ab_test()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[TrainingPipeline] Monitor error: {e}")

    async def _check_job_status(self, job: TrainingJob) -> None:
        """Check training job status."""
        if not self._reactor_client:
            return

        try:
            result = await self._reactor_client.get_job_status(job.job_id)

            if result.get("status"):
                job.status = TrainingJobStatus(result["status"])
                job.progress = result.get("progress", job.progress)
                job.metrics = result.get("metrics", job.metrics)

                if job.status == TrainingJobStatus.COMPLETED:
                    job.completed_at = datetime.now()
                    job.output_model_path = result.get("model_path")
                    self._stats.training_jobs_completed += 1

                    # Create model version
                    version = ModelVersion(
                        model_name=job.model_name,
                        training_job_id=job.job_id,
                        model_path=job.output_model_path or "",
                        performance_metrics=job.metrics,
                    )
                    self._model_versions[version.version_id] = version

                    # Move to history
                    self._job_history.append(job)
                    del self._active_jobs[job.job_id]

                    # Publish event
                    if self._event_bus:
                        from backend.core.trinity_event_bus import TrinityEvent, EventPriority
                        await self._event_bus.publish(TrinityEvent(
                            topic="training.completed",
                            payload={
                                "job_id": job.job_id,
                                "version_id": version.version_id,
                                "metrics": job.metrics,
                            },
                            priority=EventPriority.HIGH,
                        ))

                    logger.info(
                        f"[TrainingPipeline] Training completed: {job.job_id} "
                        f"→ version {version.version_id}"
                    )

                    # Auto-deploy if configured
                    if PipelineConfig.MODEL_A_B_TEST_ENABLED:
                        await self.deploy_model(version.version_id, ab_test=True)

                elif job.status == TrainingJobStatus.FAILED:
                    job.completed_at = datetime.now()
                    job.error_message = result.get("error")

                    self._job_history.append(job)
                    del self._active_jobs[job.job_id]

                    logger.warning(f"[TrainingPipeline] Training failed: {job.error_message}")

        except Exception as e:
            logger.warning(f"[TrainingPipeline] Status check error: {e}")

    async def _evaluate_ab_test(self) -> None:
        """Evaluate A/B test results."""
        if not self._ab_test_model or not self._active_model:
            return

        ab_version = self._model_versions.get(self._ab_test_model)
        active_version = self._model_versions.get(self._active_model)

        if not ab_version or not active_version:
            return

        # Get performance metrics from Prime
        if self._prime_client:
            try:
                metrics = await self._prime_client.get_ab_test_metrics()

                ab_score = metrics.get("ab_model_score", 0)
                active_score = metrics.get("active_model_score", 0)

                # Update version metrics
                ab_version.performance_metrics["ab_test_score"] = ab_score
                active_version.performance_metrics["ab_test_score"] = active_score

                # Decision logic
                if ab_score > active_score * 1.05:  # 5% improvement
                    # Promote A/B model to active
                    logger.info(
                        f"[TrainingPipeline] A/B test winner: {self._ab_test_model} "
                        f"(score: {ab_score:.3f} vs {active_score:.3f})"
                    )
                    await self.deploy_model(self._ab_test_model, ab_test=False)
                    self._ab_test_model = None

                elif ab_score < active_score * PipelineConfig.MODEL_ROLLBACK_THRESHOLD:
                    # A/B model performing poorly, stop test
                    logger.info(
                        f"[TrainingPipeline] A/B test failed: {self._ab_test_model} "
                        f"(score: {ab_score:.3f} vs {active_score:.3f})"
                    )
                    ab_version.status = ModelStatus.DEPRECATED
                    self._ab_test_model = None

            except Exception as e:
                logger.warning(f"[TrainingPipeline] A/B evaluation error: {e}")

    async def _handle_training_event(self, event: Any) -> None:
        """Handle training events from event bus."""
        try:
            if event.topic == "training.job_status":
                job_id = event.payload.get("job_id")
                if job_id in self._active_jobs:
                    self._active_jobs[job_id].status = TrainingJobStatus(
                        event.payload.get("status", "pending")
                    )
                    self._active_jobs[job_id].progress = event.payload.get("progress", 0)
        except Exception as e:
            logger.exception(f"[TrainingPipeline] Event handling error: {e}")

    async def _handle_model_event(self, event: Any) -> None:
        """Handle model events from event bus."""
        try:
            if event.topic == "model.hot_swap_complete":
                version_id = event.payload.get("version_id")
                if version_id in self._model_versions:
                    self._model_versions[version_id].status = ModelStatus.ACTIVE
                    logger.info(f"[TrainingPipeline] Hot-swap complete for {version_id}")
        except Exception as e:
            logger.exception(f"[TrainingPipeline] Event handling error: {e}")

    def get_stats(self) -> PipelineStats:
        """Get pipeline statistics."""
        return self._stats

    async def shutdown(self) -> None:
        """Shutdown the pipeline."""
        logger.info("[TrainingPipeline] Shutting down...")
        self._running = False

        # Cancel tasks
        for task in [self._flush_task, self._training_task, self._monitor_task]:
            if task:
                task.cancel()

        # Final flush
        if self._experience_buffer:
            await self._flush_experiences()

        logger.info("[TrainingPipeline] Shutdown complete")


# =============================================================================
# Global Instance
# =============================================================================

_pipeline: Optional[TrinityTrainingPipeline] = None


async def get_training_pipeline() -> TrinityTrainingPipeline:
    """Get or create the global training pipeline."""
    global _pipeline

    if _pipeline is None:
        _pipeline = await TrinityTrainingPipeline.create()

    return _pipeline


async def shutdown_training_pipeline() -> None:
    """Shutdown the global pipeline."""
    global _pipeline

    if _pipeline:
        await _pipeline.shutdown()
        _pipeline = None
