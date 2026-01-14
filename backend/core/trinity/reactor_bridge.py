"""
Reactor Core Bridge - Integration with Training Pipeline
=========================================================

Provides integration between JARVIS and Reactor Core for:
- Publishing MODEL_READY events when training completes
- Receiving experience batches from JARVIS
- Training pipeline coordination
- Model artifact management

This module can be deployed to Reactor Core repo or used from JARVIS
to communicate with Reactor Core.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                    Reactor Core Bridge                       │
    ├─────────────────────────────────────────────────────────────┤
    │  ┌─────────────────┐      ┌─────────────────────────────┐   │
    │  │    Publisher    │      │        Receiver              │   │
    │  │  (MODEL_READY,  │      │  (EXPERIENCE_BATCH,         │   │
    │  │   TRAINING_*)   │      │   STATE_SYNC_*)             │   │
    │  └────────┬────────┘      └────────────┬────────────────┘   │
    │           │                            │                     │
    │           ▼                            ▼                     │
    │  ┌─────────────────────────────────────────────────────────┐│
    │  │              File-Based IPC Layer                        ││
    │  │  ~/.jarvis/trinity/events/  ←→  ~/.jarvis/reactor/events/││
    │  └─────────────────────────────────────────────────────────┘│
    │           │                            │                     │
    │           ▼                            ▼                     │
    │  ┌─────────────────┐      ┌─────────────────────────────┐   │
    │  │ Training Pipeline│      │    Experience Ingestion     │   │
    │  │   Integration    │      │       Pipeline              │   │
    │  └─────────────────┘      └─────────────────────────────┘   │
    └─────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from backend.core.trinity.integration_coordinator import (
    EventType,
    RepoType,
    SequencedEvent,
    EventSequencer,
    CausalEventDelivery,
    ExperienceValidator,
)
from backend.core.resilience import (
    AtomicFileOps,
    DistributedDedup,
    get_distributed_dedup,
    VectorClock,
)

logger = logging.getLogger("ReactorCoreBridge")


# =============================================================================
# Configuration
# =============================================================================

TRINITY_EVENTS_DIR = Path(os.getenv("TRINITY_EVENTS_DIR", os.path.expanduser("~/.jarvis/trinity/events")))
REACTOR_EVENTS_DIR = Path(os.getenv("REACTOR_EVENTS_DIR", os.path.expanduser("~/.jarvis/reactor/events")))
REACTOR_MODELS_DIR = Path(os.getenv("REACTOR_MODELS_DIR", os.path.expanduser("~/.jarvis/reactor/models")))

# File watching
FILE_WATCH_DEBOUNCE = float(os.getenv("FILE_WATCH_DEBOUNCE", "0.5"))
FILE_WATCH_POLL_INTERVAL = float(os.getenv("FILE_WATCH_POLL_INTERVAL", "1.0"))

# Event publishing
PUBLISH_RETRY_ATTEMPTS = int(os.getenv("PUBLISH_RETRY_ATTEMPTS", "3"))
PUBLISH_RETRY_DELAY = float(os.getenv("PUBLISH_RETRY_DELAY", "1.0"))


# =============================================================================
# Model Artifact Manager
# =============================================================================

@dataclass
class ModelArtifact:
    """Represents a trained model artifact."""
    model_id: str
    model_path: Path
    model_type: str
    training_id: str
    metrics: Dict[str, float]
    config: Dict[str, Any]
    created_at: float = field(default_factory=time.time)
    checksum: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "model_id": self.model_id,
            "model_path": str(self.model_path),
            "model_type": self.model_type,
            "training_id": self.training_id,
            "metrics": self.metrics,
            "config": self.config,
            "created_at": self.created_at,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelArtifact":
        """Deserialize from dictionary."""
        return cls(
            model_id=data["model_id"],
            model_path=Path(data["model_path"]),
            model_type=data["model_type"],
            training_id=data["training_id"],
            metrics=data["metrics"],
            config=data.get("config", {}),
            created_at=data.get("created_at", time.time()),
            checksum=data.get("checksum"),
        )

    def compute_checksum(self) -> str:
        """Compute checksum of model file."""
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        hasher = hashlib.sha256()
        with open(self.model_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)

        self.checksum = hasher.hexdigest()
        return self.checksum


class ModelArtifactManager:
    """
    Manages trained model artifacts.

    Features:
    - Model registration and tracking
    - Checksum verification
    - Artifact cleanup
    - Version history
    """

    def __init__(self, models_dir: Path = REACTOR_MODELS_DIR):
        self._models_dir = models_dir
        self._artifacts: Dict[str, ModelArtifact] = {}
        self._artifact_history: List[ModelArtifact] = []

    async def initialize(self) -> None:
        """Initialize the artifact manager."""
        self._models_dir.mkdir(parents=True, exist_ok=True)

        # Load existing artifacts
        manifest_path = self._models_dir / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    data = json.load(f)
                    for artifact_data in data.get("artifacts", []):
                        artifact = ModelArtifact.from_dict(artifact_data)
                        self._artifacts[artifact.model_id] = artifact
            except Exception as e:
                logger.warning(f"Failed to load artifact manifest: {e}")

    async def register_artifact(
        self,
        model_path: Path,
        model_type: str,
        training_id: str,
        metrics: Dict[str, float],
        config: Optional[Dict[str, Any]] = None,
    ) -> ModelArtifact:
        """Register a new model artifact."""
        model_id = f"{model_type}_{training_id}_{uuid.uuid4().hex[:8]}"

        artifact = ModelArtifact(
            model_id=model_id,
            model_path=model_path,
            model_type=model_type,
            training_id=training_id,
            metrics=metrics,
            config=config or {},
        )

        # Compute checksum
        artifact.compute_checksum()

        # Store artifact
        self._artifacts[model_id] = artifact
        self._artifact_history.append(artifact)

        # Save manifest
        await self._save_manifest()

        logger.info(f"Registered model artifact: {model_id}")
        return artifact

    async def get_artifact(self, model_id: str) -> Optional[ModelArtifact]:
        """Get a model artifact by ID."""
        return self._artifacts.get(model_id)

    async def verify_artifact(self, model_id: str) -> bool:
        """Verify artifact checksum."""
        artifact = self._artifacts.get(model_id)
        if not artifact:
            return False

        if not artifact.model_path.exists():
            return False

        current_checksum = artifact.compute_checksum()
        return current_checksum == artifact.checksum

    async def _save_manifest(self) -> None:
        """Save artifact manifest to disk."""
        manifest_path = self._models_dir / "manifest.json"
        manifest_data = {
            "artifacts": [a.to_dict() for a in self._artifacts.values()],
            "updated_at": time.time(),
        }

        atomic_ops = AtomicFileOps(str(self._models_dir))
        await atomic_ops.write_json("manifest.json", manifest_data)


# =============================================================================
# Reactor Core Publisher
# =============================================================================

class ReactorCorePublisher:
    """
    Publishes events from Reactor Core to JARVIS.

    Handles MODEL_READY, TRAINING_* events.
    """

    def __init__(
        self,
        events_dir: Path = REACTOR_EVENTS_DIR,
        artifact_manager: Optional[ModelArtifactManager] = None,
    ):
        self._events_dir = events_dir
        self._artifact_manager = artifact_manager or ModelArtifactManager()
        self._sequencer = EventSequencer("reactor_core")
        self._atomic_ops = AtomicFileOps(str(events_dir))

    async def initialize(self) -> None:
        """Initialize the publisher."""
        self._events_dir.mkdir(parents=True, exist_ok=True)
        await self._artifact_manager.initialize()

    async def publish_model_ready(
        self,
        model_path: Path,
        model_type: str,
        training_id: str,
        metrics: Dict[str, float],
        config: Optional[Dict[str, Any]] = None,
    ) -> SequencedEvent:
        """
        Publish MODEL_READY event after training completes.

        This is the critical event that triggers model deployment in JARVIS.
        """
        # Register artifact
        artifact = await self._artifact_manager.register_artifact(
            model_path=model_path,
            model_type=model_type,
            training_id=training_id,
            metrics=metrics,
            config=config,
        )

        # Create event
        event = await self._sequencer.create_event(
            event_type=EventType.MODEL_READY,
            payload={
                "model_id": artifact.model_id,
                "model_path": str(artifact.model_path),
                "model_type": artifact.model_type,
                "training_id": artifact.training_id,
                "metrics": artifact.metrics,
                "config": artifact.config,
                "checksum": artifact.checksum,
            },
        )

        # Write event file
        await self._write_event(event)

        logger.info(f"Published MODEL_READY: {artifact.model_id}")
        return event

    async def publish_training_started(
        self,
        training_id: str,
        model_type: str,
        config: Dict[str, Any],
    ) -> SequencedEvent:
        """Publish TRAINING_STARTED event."""
        event = await self._sequencer.create_event(
            event_type=EventType.TRAINING_STARTED,
            payload={
                "training_id": training_id,
                "model_type": model_type,
                "config": config,
                "started_at": time.time(),
            },
        )

        await self._write_event(event)
        logger.info(f"Published TRAINING_STARTED: {training_id}")
        return event

    async def publish_training_progress(
        self,
        training_id: str,
        epoch: int,
        total_epochs: int,
        metrics: Dict[str, float],
    ) -> SequencedEvent:
        """Publish TRAINING_PROGRESS event."""
        event = await self._sequencer.create_event(
            event_type=EventType.TRAINING_PROGRESS,
            payload={
                "training_id": training_id,
                "epoch": epoch,
                "total_epochs": total_epochs,
                "progress": epoch / total_epochs,
                "metrics": metrics,
            },
        )

        await self._write_event(event)
        return event

    async def publish_training_completed(
        self,
        training_id: str,
        model_id: str,
        duration: float,
        final_metrics: Dict[str, float],
    ) -> SequencedEvent:
        """Publish TRAINING_COMPLETED event."""
        event = await self._sequencer.create_event(
            event_type=EventType.TRAINING_COMPLETED,
            payload={
                "training_id": training_id,
                "model_id": model_id,
                "duration": duration,
                "final_metrics": final_metrics,
                "completed_at": time.time(),
            },
        )

        await self._write_event(event)
        logger.info(f"Published TRAINING_COMPLETED: {training_id}")
        return event

    async def publish_training_failed(
        self,
        training_id: str,
        error: str,
        partial_metrics: Optional[Dict[str, float]] = None,
    ) -> SequencedEvent:
        """Publish TRAINING_FAILED event."""
        event = await self._sequencer.create_event(
            event_type=EventType.TRAINING_FAILED,
            payload={
                "training_id": training_id,
                "error": error,
                "partial_metrics": partial_metrics or {},
                "failed_at": time.time(),
            },
        )

        await self._write_event(event)
        logger.error(f"Published TRAINING_FAILED: {training_id} - {error}")
        return event

    async def _write_event(self, event: SequencedEvent) -> None:
        """Write event to file."""
        filename = f"{int(event.timestamp * 1000)}_{event.event_id}.json"
        await self._atomic_ops.write_json(filename, event.to_dict())


# =============================================================================
# Reactor Core Receiver
# =============================================================================

class ReactorCoreReceiver:
    """
    Receives events from JARVIS in Reactor Core.

    Handles EXPERIENCE_BATCH, STATE_SYNC_* events.
    """

    def __init__(
        self,
        watch_dir: Path = TRINITY_EVENTS_DIR,
        redis_client: Optional[Any] = None,
    ):
        self._watch_dir = watch_dir
        self._redis = redis_client
        self._sequencer = EventSequencer("reactor_core")
        self._causal_delivery = CausalEventDelivery()
        self._experience_validator = ExperienceValidator()
        self._dedup: Optional[DistributedDedup] = None

        # File watching
        self._observer: Optional[Observer] = None
        self._running = False
        self._processed_files: Set[str] = set()

        # Callbacks
        self._experience_callbacks: List[Callable] = []

    async def initialize(self) -> None:
        """Initialize the receiver."""
        self._watch_dir.mkdir(parents=True, exist_ok=True)

        if self._redis:
            self._dedup = await get_distributed_dedup(self._redis)

        # Register handlers
        self._causal_delivery.register_handler(
            EventType.EXPERIENCE_BATCH,
            self._handle_experience_batch,
        )

    async def start(self) -> None:
        """Start watching for events."""
        self._running = True

        # Process existing files
        await self._process_existing_files()

        # Start file watcher
        event_handler = _FileWatchHandler(self._on_file_created)
        self._observer = Observer()
        self._observer.schedule(event_handler, str(self._watch_dir), recursive=False)
        self._observer.start()

        logger.info(f"ReactorCoreReceiver started watching {self._watch_dir}")

    async def stop(self) -> None:
        """Stop watching for events."""
        self._running = False

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)

        logger.info("ReactorCoreReceiver stopped")

    def on_experience_batch(
        self,
        callback: Callable[[List[Dict[str, Any]]], Coroutine[Any, Any, None]],
    ) -> None:
        """Register callback for experience batches."""
        self._experience_callbacks.append(callback)

    async def _process_existing_files(self) -> None:
        """Process any existing event files on startup."""
        if not self._watch_dir.exists():
            return

        for file_path in sorted(self._watch_dir.glob("*.json")):
            if str(file_path) not in self._processed_files:
                await self._process_file(file_path)

    async def _on_file_created(self, file_path: Path) -> None:
        """Handle new file creation."""
        if not file_path.suffix == ".json":
            return

        if str(file_path) in self._processed_files:
            return

        # Debounce
        await asyncio.sleep(FILE_WATCH_DEBOUNCE)

        await self._process_file(file_path)

    async def _process_file(self, file_path: Path) -> None:
        """Process an event file."""
        try:
            with open(file_path) as f:
                event_data = json.load(f)

            event = SequencedEvent.from_dict(event_data)

            # Check for duplicates
            if self._dedup:
                if await self._dedup.is_duplicate(event.event_id):
                    logger.debug(f"Duplicate event ignored: {event.event_id}")
                    return
                await self._dedup.mark_processed(event.event_id)

            # Validate sequence
            await self._sequencer.receive_event(event)

            # Deliver through causal delivery
            await self._causal_delivery.receive(event)

            self._processed_files.add(str(file_path))
            logger.debug(f"Processed event: {event.event_id}")

        except Exception as e:
            logger.error(f"Failed to process {file_path}: {e}")

    async def _handle_experience_batch(self, event: SequencedEvent) -> None:
        """Handle EXPERIENCE_BATCH event."""
        payload = event.payload
        batch_id = payload.get("batch_id")
        experiences = payload.get("experiences", [])

        logger.info(f"Received experience batch: {batch_id} ({len(experiences)} experiences)")

        # Validate experiences
        valid, errors = await self._experience_validator.validate_batch(experiences)

        if errors:
            logger.warning(f"Experience validation errors: {errors[:5]}")

        # Notify callbacks
        for callback in self._experience_callbacks:
            try:
                await callback(valid)
            except Exception as e:
                logger.error(f"Experience callback error: {e}")

        # Send acknowledgment
        await self._send_ack(event.event_id, len(valid), len(errors))

    async def _send_ack(
        self,
        original_event_id: str,
        processed_count: int,
        error_count: int,
    ) -> None:
        """Send acknowledgment event."""
        ack_event = await self._sequencer.create_event(
            event_type=EventType.EXPERIENCE_ACK,
            payload={
                "original_event_id": original_event_id,
                "processed_count": processed_count,
                "error_count": error_count,
                "ack_at": time.time(),
            },
        )

        # Write to output directory
        output_dir = REACTOR_EVENTS_DIR
        output_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{int(ack_event.timestamp * 1000)}_{ack_event.event_id}.json"
        atomic_ops = AtomicFileOps(str(output_dir))
        await atomic_ops.write_json(filename, ack_event.to_dict())


class _FileWatchHandler(FileSystemEventHandler):
    """Watchdog event handler for file creation."""

    def __init__(self, callback: Callable[[Path], Coroutine]):
        super().__init__()
        self._callback = callback
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            file_path = Path(event.src_path)
            if file_path.suffix == ".json":
                # Schedule async callback
                if self._loop is None:
                    try:
                        self._loop = asyncio.get_running_loop()
                    except RuntimeError:
                        self._loop = asyncio.get_event_loop()

                asyncio.run_coroutine_threadsafe(
                    self._callback(file_path),
                    self._loop,
                )


# =============================================================================
# Training Pipeline Integration
# =============================================================================

class TrainingPipelineIntegration:
    """
    Integrates with Reactor Core training pipeline.

    Provides hooks for training lifecycle events and automatic
    MODEL_READY publishing.
    """

    def __init__(self, publisher: Optional[ReactorCorePublisher] = None):
        self._publisher = publisher or ReactorCorePublisher()
        self._active_trainings: Dict[str, Dict[str, Any]] = {}

    async def initialize(self) -> None:
        """Initialize the integration."""
        await self._publisher.initialize()

    async def on_training_start(
        self,
        training_id: str,
        model_type: str,
        config: Dict[str, Any],
    ) -> None:
        """
        Call when training starts.

        This method should be called from the training pipeline
        when a new training run begins.
        """
        self._active_trainings[training_id] = {
            "model_type": model_type,
            "config": config,
            "started_at": time.time(),
            "epochs_completed": 0,
        }

        await self._publisher.publish_training_started(
            training_id=training_id,
            model_type=model_type,
            config=config,
        )

    async def on_epoch_complete(
        self,
        training_id: str,
        epoch: int,
        total_epochs: int,
        metrics: Dict[str, float],
    ) -> None:
        """
        Call when an epoch completes.

        Reports progress to JARVIS.
        """
        if training_id in self._active_trainings:
            self._active_trainings[training_id]["epochs_completed"] = epoch

        await self._publisher.publish_training_progress(
            training_id=training_id,
            epoch=epoch,
            total_epochs=total_epochs,
            metrics=metrics,
        )

    async def on_training_complete(
        self,
        training_id: str,
        model_path: Path,
        final_metrics: Dict[str, float],
    ) -> str:
        """
        Call when training completes successfully.

        Publishes MODEL_READY event and returns the model_id.

        This is the CRITICAL method that triggers model deployment.
        """
        if training_id not in self._active_trainings:
            raise ValueError(f"Unknown training_id: {training_id}")

        training_info = self._active_trainings[training_id]
        duration = time.time() - training_info["started_at"]

        # Publish MODEL_READY - this triggers deployment in JARVIS
        event = await self._publisher.publish_model_ready(
            model_path=model_path,
            model_type=training_info["model_type"],
            training_id=training_id,
            metrics=final_metrics,
            config=training_info["config"],
        )

        # Also publish TRAINING_COMPLETED
        model_id = event.payload["model_id"]
        await self._publisher.publish_training_completed(
            training_id=training_id,
            model_id=model_id,
            duration=duration,
            final_metrics=final_metrics,
        )

        # Cleanup
        del self._active_trainings[training_id]

        return model_id

    async def on_training_failed(
        self,
        training_id: str,
        error: str,
        partial_metrics: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Call when training fails.

        Reports failure to JARVIS.
        """
        await self._publisher.publish_training_failed(
            training_id=training_id,
            error=error,
            partial_metrics=partial_metrics,
        )

        # Cleanup
        if training_id in self._active_trainings:
            del self._active_trainings[training_id]


# =============================================================================
# Reactor Core Bridge (Main Class)
# =============================================================================

class ReactorCoreBridge:
    """
    Main bridge between JARVIS and Reactor Core.

    Combines publisher, receiver, and training integration.
    """

    def __init__(self, redis_client: Optional[Any] = None):
        self._redis = redis_client
        self._publisher = ReactorCorePublisher()
        self._receiver = ReactorCoreReceiver(redis_client=redis_client)
        self._training = TrainingPipelineIntegration(self._publisher)
        self._running = False

    async def initialize(self) -> None:
        """Initialize all components."""
        await self._publisher.initialize()
        await self._receiver.initialize()
        await self._training.initialize()

    async def start(self) -> None:
        """Start the bridge."""
        self._running = True
        await self._receiver.start()
        logger.info("ReactorCoreBridge started")

    async def stop(self) -> None:
        """Stop the bridge."""
        self._running = False
        await self._receiver.stop()
        logger.info("ReactorCoreBridge stopped")

    @property
    def publisher(self) -> ReactorCorePublisher:
        """Get the publisher."""
        return self._publisher

    @property
    def receiver(self) -> ReactorCoreReceiver:
        """Get the receiver."""
        return self._receiver

    @property
    def training(self) -> TrainingPipelineIntegration:
        """Get the training integration."""
        return self._training

    def on_experience_batch(
        self,
        callback: Callable[[List[Dict[str, Any]]], Coroutine[Any, Any, None]],
    ) -> None:
        """Register callback for experience batches."""
        self._receiver.on_experience_batch(callback)


# =============================================================================
# Global Factory
# =============================================================================

_bridge_instance: Optional[ReactorCoreBridge] = None
_bridge_lock = asyncio.Lock()


async def get_reactor_bridge(
    redis_client: Optional[Any] = None,
) -> ReactorCoreBridge:
    """Get or create the global ReactorCoreBridge instance."""
    global _bridge_instance

    async with _bridge_lock:
        if _bridge_instance is None:
            _bridge_instance = ReactorCoreBridge(redis_client=redis_client)
            await _bridge_instance.initialize()
            await _bridge_instance.start()

        return _bridge_instance


async def shutdown_reactor_bridge() -> None:
    """Shutdown the global bridge."""
    global _bridge_instance

    if _bridge_instance:
        await _bridge_instance.stop()
        _bridge_instance = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "ReactorCoreBridge",
    "ReactorCorePublisher",
    "ReactorCoreReceiver",
    "TrainingPipelineIntegration",
    "ModelArtifactManager",
    "ModelArtifact",
    "get_reactor_bridge",
    "shutdown_reactor_bridge",
]
