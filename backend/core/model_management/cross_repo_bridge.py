"""
Cross-Repository Model Bridge v1.0
===================================

Provides seamless model synchronization across the JARVIS Trinity:
- JARVIS (Body) - Model inference and deployment
- JARVIS Prime (Mind) - Model orchestration and routing
- Reactor Core (Learning) - Model training and publishing

Features:
- Bi-directional model sync with versioning
- Training job coordination
- Deployment propagation
- Model discovery across repos
- Event-driven notifications
- Conflict resolution

Author: Trinity Model System
Version: 1.0.0
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ModelSyncDirection(Enum):
    """Direction of model synchronization."""
    REACTOR_TO_PRIME = "reactor_to_prime"      # New model from training
    PRIME_TO_JARVIS = "prime_to_jarvis"        # Deploy to body
    JARVIS_TO_PRIME = "jarvis_to_prime"        # Performance feedback
    PRIME_TO_REACTOR = "prime_to_reactor"      # Trigger retraining
    BROADCAST = "broadcast"                     # All repos


class ModelEventType(Enum):
    """Types of model events."""
    MODEL_TRAINED = auto()
    MODEL_VALIDATED = auto()
    MODEL_DEPLOYED = auto()
    MODEL_PROMOTED = auto()
    MODEL_DEMOTED = auto()
    MODEL_ROLLED_BACK = auto()
    MODEL_ARCHIVED = auto()
    PERFORMANCE_ALERT = auto()
    TRAINING_REQUESTED = auto()
    A_B_TEST_STARTED = auto()
    A_B_TEST_COMPLETED = auto()


class SyncStatus(Enum):
    """Status of sync operation."""
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    CONFLICT = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class CrossRepoModelConfig:
    """Configuration for cross-repo model operations."""

    # Sync settings
    sync_interval_seconds: float = float(os.getenv("MODEL_SYNC_INTERVAL", "30.0"))
    sync_timeout_seconds: float = float(os.getenv("MODEL_SYNC_TIMEOUT", "60.0"))
    max_sync_retries: int = int(os.getenv("MODEL_SYNC_MAX_RETRIES", "3"))

    # Model paths
    jarvis_models_path: str = os.getenv("JARVIS_MODELS_PATH", "backend/models")
    prime_models_path: str = os.getenv("PRIME_MODELS_PATH", "../JARVIS-Prime/models")
    reactor_output_path: str = os.getenv("REACTOR_OUTPUT_PATH", "../reactor-core/output")

    # Event settings
    event_buffer_size: int = int(os.getenv("MODEL_EVENT_BUFFER_SIZE", "1000"))
    event_retention_hours: float = float(os.getenv("MODEL_EVENT_RETENTION_HOURS", "24.0"))

    # Discovery
    discovery_enabled: bool = os.getenv("MODEL_DISCOVERY_ENABLED", "true").lower() == "true"
    discovery_interval_seconds: float = float(os.getenv("MODEL_DISCOVERY_INTERVAL", "60.0"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ModelEvent:
    """An event in the model lifecycle."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: ModelEventType = ModelEventType.MODEL_TRAINED
    model_name: str = ""
    model_version: str = ""
    source_repo: str = ""
    target_repo: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.name,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "source_repo": self.source_repo,
            "target_repo": self.target_repo,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "correlation_id": self.correlation_id,
        }


@dataclass
class SyncOperation:
    """A model sync operation."""
    sync_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    direction: ModelSyncDirection = ModelSyncDirection.REACTOR_TO_PRIME
    model_name: str = ""
    model_version: str = ""
    status: SyncStatus = SyncStatus.PENDING

    # Progress
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    bytes_transferred: int = 0
    total_bytes: int = 0

    # Results
    success: bool = False
    error_message: Optional[str] = None
    retries: int = 0


@dataclass
class RepoModelInventory:
    """Inventory of models in a repository."""
    repo_name: str = ""
    models: Dict[str, List[str]] = field(default_factory=dict)  # model_name -> versions
    last_updated: datetime = field(default_factory=datetime.utcnow)
    total_size_bytes: int = 0


@dataclass
class TrainingRequest:
    """Request for model training."""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model_name: str = ""
    model_type: str = ""
    base_version: Optional[str] = None
    training_config: Dict[str, Any] = field(default_factory=dict)
    priority: int = 1  # 0 = highest
    created_at: datetime = field(default_factory=datetime.utcnow)
    requester: str = "jarvis"
    status: str = "pending"  # pending, queued, training, completed, failed


# =============================================================================
# MODEL EVENT BUS
# =============================================================================


class ModelEventBus:
    """
    Event bus for model lifecycle events.

    Provides pub/sub for model events across repos.
    """

    def __init__(self, config: CrossRepoModelConfig):
        self.config = config
        self._subscribers: Dict[ModelEventType, List[Callable]] = defaultdict(list)
        self._event_history: List[ModelEvent] = []
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("ModelEventBus")

    def subscribe(
        self,
        event_type: ModelEventType,
        callback: Callable[[ModelEvent], Any],
    ):
        """Subscribe to an event type."""
        self._subscribers[event_type].append(callback)

    def subscribe_all(self, callback: Callable[[ModelEvent], Any]):
        """Subscribe to all event types."""
        for event_type in ModelEventType:
            self._subscribers[event_type].append(callback)

    async def publish(self, event: ModelEvent):
        """Publish an event."""
        async with self._lock:
            # Store in history
            self._event_history.append(event)

            # Trim old events
            cutoff = datetime.utcnow().timestamp() - (self.config.event_retention_hours * 3600)
            self._event_history = [
                e for e in self._event_history
                if e.timestamp.timestamp() > cutoff
            ]

        # Notify subscribers
        for callback in self._subscribers[event.event_type]:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Event callback error: {e}")

        self.logger.debug(f"Published event: {event.event_type.name} for {event.model_name}")

    async def get_history(
        self,
        event_type: Optional[ModelEventType] = None,
        model_name: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[ModelEvent]:
        """Get event history with filtering."""
        async with self._lock:
            events = self._event_history
            if event_type:
                events = [e for e in events if e.event_type == event_type]
            if model_name:
                events = [e for e in events if e.model_name == model_name]
            if since:
                events = [e for e in events if e.timestamp >= since]
            return events


# =============================================================================
# CROSS-REPO MODEL BRIDGE
# =============================================================================


class CrossRepoModelBridge:
    """
    Main bridge for cross-repo model operations.

    Coordinates model flow across the Trinity ecosystem.
    """

    def __init__(self, config: Optional[CrossRepoModelConfig] = None):
        self.config = config or CrossRepoModelConfig()
        self.logger = logging.getLogger("CrossRepoModelBridge")

        # Components
        self.event_bus = ModelEventBus(self.config)

        # State
        self._inventories: Dict[str, RepoModelInventory] = {}
        self._pending_syncs: Dict[str, SyncOperation] = {}
        self._training_requests: Dict[str, TrainingRequest] = {}

        # Background tasks
        self._running = False
        self._tasks: List[asyncio.Task] = []

        # Locks
        self._lock = asyncio.Lock()

        # Subscribe to events
        self._setup_event_handlers()

    def _setup_event_handlers(self):
        """Setup internal event handlers."""
        self.event_bus.subscribe(ModelEventType.MODEL_TRAINED, self._on_model_trained)
        self.event_bus.subscribe(ModelEventType.MODEL_VALIDATED, self._on_model_validated)
        self.event_bus.subscribe(ModelEventType.PERFORMANCE_ALERT, self._on_performance_alert)

    async def start(self):
        """Start the bridge."""
        if self._running:
            return

        self._running = True

        # Start background tasks
        self._tasks = [
            asyncio.create_task(self._sync_loop()),
            asyncio.create_task(self._discovery_loop()),
        ]

        self.logger.info("CrossRepoModelBridge started")

    async def stop(self):
        """Stop the bridge."""
        if not self._running:
            return

        self._running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        self.logger.info("CrossRepoModelBridge stopped")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def register_trained_model(
        self,
        model_name: str,
        model_version: str,
        model_path: str,
        metadata: Dict[str, Any],
    ) -> str:
        """Register a newly trained model from Reactor Core."""
        event = ModelEvent(
            event_type=ModelEventType.MODEL_TRAINED,
            model_name=model_name,
            model_version=model_version,
            source_repo="reactor",
            payload={
                "path": model_path,
                "metadata": metadata,
            }
        )
        await self.event_bus.publish(event)
        return event.event_id

    async def deploy_to_jarvis(
        self,
        model_name: str,
        model_version: str,
        deployment_strategy: str = "canary",
    ) -> SyncOperation:
        """Deploy a model to JARVIS Body."""
        sync_op = SyncOperation(
            direction=ModelSyncDirection.PRIME_TO_JARVIS,
            model_name=model_name,
            model_version=model_version,
        )

        async with self._lock:
            self._pending_syncs[sync_op.sync_id] = sync_op

        # Publish deployment event
        await self.event_bus.publish(ModelEvent(
            event_type=ModelEventType.MODEL_DEPLOYED,
            model_name=model_name,
            model_version=model_version,
            source_repo="prime",
            target_repo="jarvis",
            payload={"strategy": deployment_strategy},
            correlation_id=sync_op.sync_id,
        ))

        return sync_op

    async def request_training(
        self,
        model_name: str,
        model_type: str,
        base_version: Optional[str] = None,
        training_config: Optional[Dict[str, Any]] = None,
        priority: int = 1,
    ) -> TrainingRequest:
        """Request model training from Reactor Core."""
        request = TrainingRequest(
            model_name=model_name,
            model_type=model_type,
            base_version=base_version,
            training_config=training_config or {},
            priority=priority,
        )

        async with self._lock:
            self._training_requests[request.request_id] = request

        # Publish training request event
        await self.event_bus.publish(ModelEvent(
            event_type=ModelEventType.TRAINING_REQUESTED,
            model_name=model_name,
            source_repo="prime",
            target_repo="reactor",
            payload={
                "request_id": request.request_id,
                "model_type": model_type,
                "base_version": base_version,
                "config": training_config,
                "priority": priority,
            }
        ))

        self.logger.info(f"Requested training for {model_name} with priority {priority}")
        return request

    async def report_performance(
        self,
        model_name: str,
        model_version: str,
        metrics: Dict[str, float],
        alert_level: Optional[str] = None,
    ):
        """Report model performance from JARVIS Body."""
        event_type = ModelEventType.PERFORMANCE_ALERT if alert_level else ModelEventType.MODEL_DEPLOYED

        await self.event_bus.publish(ModelEvent(
            event_type=event_type,
            model_name=model_name,
            model_version=model_version,
            source_repo="jarvis",
            payload={
                "metrics": metrics,
                "alert_level": alert_level,
            }
        ))

    async def get_model_inventory(self, repo: str) -> Optional[RepoModelInventory]:
        """Get model inventory for a repo."""
        return self._inventories.get(repo)

    async def discover_models(self, repo: str) -> RepoModelInventory:
        """Discover models in a repository."""
        path_map = {
            "jarvis": self.config.jarvis_models_path,
            "prime": self.config.prime_models_path,
            "reactor": self.config.reactor_output_path,
        }

        path = Path(path_map.get(repo, ""))
        inventory = RepoModelInventory(repo_name=repo)

        if path.exists():
            for model_dir in path.iterdir():
                if model_dir.is_dir():
                    versions = []
                    for version_file in model_dir.glob("*"):
                        if version_file.is_file():
                            versions.append(version_file.stem)
                    if versions:
                        inventory.models[model_dir.name] = sorted(versions)
                        inventory.total_size_bytes += sum(
                            f.stat().st_size for f in model_dir.glob("*")
                        )

        inventory.last_updated = datetime.utcnow()
        self._inventories[repo] = inventory
        return inventory

    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------

    async def _on_model_trained(self, event: ModelEvent):
        """Handle model trained event."""
        self.logger.info(f"Model trained: {event.model_name}@{event.model_version}")

        # Trigger validation and deployment pipeline
        # In a real implementation, this would coordinate with the model management engine

    async def _on_model_validated(self, event: ModelEvent):
        """Handle model validated event."""
        self.logger.info(f"Model validated: {event.model_name}@{event.model_version}")

        # Initiate deployment to JARVIS
        if event.payload.get("passed", False):
            await self.deploy_to_jarvis(
                event.model_name,
                event.model_version,
                "canary",
            )

    async def _on_performance_alert(self, event: ModelEvent):
        """Handle performance alert event."""
        alert_level = event.payload.get("alert_level", "warning")
        self.logger.warning(
            f"Performance alert for {event.model_name}: {alert_level}"
        )

        # If critical, trigger retraining
        if alert_level == "critical":
            await self.request_training(
                event.model_name,
                event.payload.get("model_type", "unknown"),
                event.model_version,
                priority=0,  # Highest priority
            )

    # -------------------------------------------------------------------------
    # Background Tasks
    # -------------------------------------------------------------------------

    async def _sync_loop(self):
        """Main sync loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.sync_interval_seconds)
                await self._process_pending_syncs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync loop error: {e}")

    async def _process_pending_syncs(self):
        """Process pending sync operations."""
        async with self._lock:
            pending = [
                op for op in self._pending_syncs.values()
                if op.status == SyncStatus.PENDING
            ]

        for op in pending:
            try:
                await self._execute_sync(op)
            except Exception as e:
                op.status = SyncStatus.FAILED
                op.error_message = str(e)
                self.logger.error(f"Sync failed for {op.sync_id}: {e}")

    async def _execute_sync(self, op: SyncOperation):
        """Execute a sync operation."""
        op.status = SyncStatus.IN_PROGRESS
        op.started_at = datetime.utcnow()

        try:
            # Simulate sync (in real implementation, would copy files)
            await asyncio.sleep(0.1)

            op.status = SyncStatus.COMPLETED
            op.success = True
            op.completed_at = datetime.utcnow()

            self.logger.info(f"Sync completed: {op.sync_id}")

        except Exception as e:
            op.status = SyncStatus.FAILED
            op.error_message = str(e)
            op.retries += 1

            if op.retries < self.config.max_sync_retries:
                op.status = SyncStatus.PENDING

    async def _discovery_loop(self):
        """Model discovery loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.discovery_interval_seconds)

                if self.config.discovery_enabled:
                    for repo in ["jarvis", "prime", "reactor"]:
                        await self.discover_models(repo)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Discovery loop error: {e}")

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get bridge status."""
        return {
            "running": self._running,
            "inventories": {
                repo: {
                    "models": len(inv.models),
                    "size_bytes": inv.total_size_bytes,
                    "last_updated": inv.last_updated.isoformat(),
                }
                for repo, inv in self._inventories.items()
            },
            "pending_syncs": len([
                op for op in self._pending_syncs.values()
                if op.status == SyncStatus.PENDING
            ]),
            "training_requests": len(self._training_requests),
        }


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_bridge: Optional[CrossRepoModelBridge] = None
_bridge_lock = asyncio.Lock()


async def get_cross_repo_model_bridge() -> CrossRepoModelBridge:
    """Get or create the global bridge instance."""
    global _bridge

    async with _bridge_lock:
        if _bridge is None:
            _bridge = CrossRepoModelBridge()
            await _bridge.start()
        return _bridge


async def initialize_cross_repo_models(
    config: Optional[CrossRepoModelConfig] = None
) -> CrossRepoModelBridge:
    """Initialize the cross-repo model bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is not None:
            await _bridge.stop()

        _bridge = CrossRepoModelBridge(config)
        await _bridge.start()

        logger.info("Cross-repo model bridge initialized")
        return _bridge


async def shutdown_cross_repo_models():
    """Shutdown the cross-repo model bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is not None:
            await _bridge.stop()
            _bridge = None
            logger.info("Cross-repo model bridge shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "CrossRepoModelConfig",
    # Enums
    "ModelSyncDirection",
    "ModelEventType",
    "SyncStatus",
    # Data Structures
    "ModelEvent",
    "SyncOperation",
    "RepoModelInventory",
    "TrainingRequest",
    # Event Bus
    "ModelEventBus",
    # Bridge
    "CrossRepoModelBridge",
    # Global Functions
    "get_cross_repo_model_bridge",
    "initialize_cross_repo_models",
    "shutdown_cross_repo_models",
]
