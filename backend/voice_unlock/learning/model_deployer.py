"""
Voice Model Deployer - Automatic Model Deployment from Reactor-Core.
=====================================================================

Handles automatic deployment of new voice models trained by Reactor-Core.
Supports A/B testing, gradual rollout, and automatic rollback.

Features:
1. Model version management
2. Hot-swap model deployment
3. A/B testing integration
4. Performance monitoring
5. Automatic rollback on degradation
6. Deployment history tracking

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  VoiceModelDeployer                                                 │
    │  ├── ModelRegistry (tracks available models)                        │
    │  ├── DeploymentManager (handles deployment logic)                   │
    │  ├── PerformanceMonitor (tracks model performance)                  │
    │  └── RollbackController (handles automatic rollback)                │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS Trinity v81.0 - Unified Learning Loop
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_path(key: str, default: str) -> Path:
    return Path(os.getenv(key, default))


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class ModelType(Enum):
    """Types of voice models."""
    ECAPA_TDNN = "ecapa_tdnn"
    SPEAKER_VERIFICATION = "speaker_verification"
    ANTI_SPOOFING = "anti_spoofing"
    LIVENESS_DETECTION = "liveness_detection"
    VOICE_ACTIVITY = "voice_activity"


class DeploymentStrategy(Enum):
    """Strategies for model deployment."""
    IMMEDIATE = "immediate"      # Replace immediately
    GRADUAL = "gradual"          # Gradual rollout
    AB_TEST = "ab_test"          # A/B testing first
    SHADOW = "shadow"            # Shadow mode (run but don't use)
    CANARY = "canary"            # Small percentage first


class DeploymentStatus(Enum):
    """Status of a deployment."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass
class ModelVersion:
    """A version of a voice model."""
    version_id: str
    model_type: ModelType
    model_path: Path
    created_at: float
    metrics: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    @property
    def age_hours(self) -> float:
        """Age of this version in hours."""
        return (time.time() - self.created_at) / 3600

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "version_id": self.version_id,
            "model_type": self.model_type.value,
            "model_path": str(self.model_path),
            "created_at": self.created_at,
            "metrics": self.metrics,
            "metadata": self.metadata,
            "checksum": self.checksum,
        }


@dataclass
class DeploymentRecord:
    """Record of a model deployment."""
    deployment_id: str
    version: ModelVersion
    strategy: DeploymentStrategy
    status: DeploymentStatus
    started_at: float
    completed_at: float = 0.0
    traffic_percentage: float = 100.0
    performance_metrics: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        """Duration of deployment."""
        end = self.completed_at or time.time()
        return end - self.started_at


@dataclass
class DeploymentResult:
    """Result of a deployment operation."""
    success: bool
    deployment_id: str
    version_id: str
    strategy: DeploymentStrategy
    elapsed_seconds: float
    previous_version: Optional[str] = None
    error: Optional[str] = None


# =============================================================================
# Model Registry
# =============================================================================

class ModelRegistry:
    """
    Registry of available voice models.

    Tracks all model versions and their metadata.
    """

    def __init__(self, models_dir: Path):
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)

        self._versions: Dict[str, Dict[str, ModelVersion]] = {}  # type -> version_id -> version
        self._active: Dict[str, str] = {}  # type -> active version_id
        self._lock = asyncio.Lock()

        self._load_registry()

    def _load_registry(self) -> None:
        """Load registry from disk."""
        registry_file = self.models_dir / "registry.json"

        if registry_file.exists():
            try:
                with open(registry_file) as f:
                    data = json.load(f)

                for model_type, versions in data.get("versions", {}).items():
                    self._versions[model_type] = {}
                    for version_id, version_data in versions.items():
                        self._versions[model_type][version_id] = ModelVersion(
                            version_id=version_data["version_id"],
                            model_type=ModelType(version_data["model_type"]),
                            model_path=Path(version_data["model_path"]),
                            created_at=version_data["created_at"],
                            metrics=version_data.get("metrics", {}),
                            metadata=version_data.get("metadata", {}),
                            checksum=version_data.get("checksum", ""),
                        )

                self._active = data.get("active", {})
                logger.info(f"[ModelRegistry] Loaded {sum(len(v) for v in self._versions.values())} versions")

            except Exception as e:
                logger.warning(f"[ModelRegistry] Failed to load registry: {e}")

    async def save_registry(self) -> None:
        """Save registry to disk."""
        async with self._lock:
            registry_file = self.models_dir / "registry.json"

            data = {
                "versions": {
                    model_type: {
                        version_id: version.to_dict()
                        for version_id, version in versions.items()
                    }
                    for model_type, versions in self._versions.items()
                },
                "active": self._active,
                "updated_at": time.time(),
            }

            try:
                with open(registry_file, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.error(f"[ModelRegistry] Failed to save registry: {e}")

    async def register_version(self, version: ModelVersion) -> None:
        """Register a new model version."""
        async with self._lock:
            model_type = version.model_type.value

            if model_type not in self._versions:
                self._versions[model_type] = {}

            self._versions[model_type][version.version_id] = version
            logger.info(
                f"[ModelRegistry] Registered {model_type} v{version.version_id}"
            )

        await self.save_registry()

    def get_version(
        self,
        model_type: ModelType,
        version_id: str,
    ) -> Optional[ModelVersion]:
        """Get a specific model version."""
        versions = self._versions.get(model_type.value, {})
        return versions.get(version_id)

    def get_active_version(self, model_type: ModelType) -> Optional[ModelVersion]:
        """Get the active version for a model type."""
        active_id = self._active.get(model_type.value)
        if active_id:
            return self.get_version(model_type, active_id)
        return None

    def get_all_versions(self, model_type: ModelType) -> List[ModelVersion]:
        """Get all versions for a model type."""
        versions = self._versions.get(model_type.value, {})
        return sorted(versions.values(), key=lambda v: v.created_at, reverse=True)

    async def set_active(self, model_type: ModelType, version_id: str) -> None:
        """Set the active version for a model type."""
        async with self._lock:
            self._active[model_type.value] = version_id
            logger.info(
                f"[ModelRegistry] Set active {model_type.value} → v{version_id}"
            )

        await self.save_registry()


# =============================================================================
# Voice Model Deployer
# =============================================================================

class VoiceModelDeployer:
    """
    Handles deployment of voice models from Reactor-Core.

    Supports multiple deployment strategies:
    - Immediate: Replace current model instantly
    - Gradual: Roll out to increasing traffic percentage
    - A/B Test: Run controlled experiment
    - Shadow: Run in parallel without affecting decisions
    - Canary: Deploy to small percentage first

    Usage:
        deployer = VoiceModelDeployer()
        result = await deployer.deploy_model(
            model_path=Path("/path/to/model"),
            model_type=ModelType.ECAPA_TDNN,
            strategy=DeploymentStrategy.AB_TEST,
        )
    """

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        enable_auto_rollback: bool = True,
        min_performance_threshold: float = 0.80,
    ):
        """
        Initialize the model deployer.

        Args:
            models_dir: Directory for model storage
            enable_auto_rollback: Enable automatic rollback on degradation
            min_performance_threshold: Minimum performance before rollback
        """
        self.models_dir = models_dir or _env_path(
            "VOICE_MODELS_DIR",
            str(Path.home() / ".jarvis" / "voice_models")
        )
        self.enable_auto_rollback = enable_auto_rollback
        self.min_performance_threshold = min_performance_threshold

        # Model registry
        self._registry = ModelRegistry(self.models_dir)

        # Deployment history
        self._deployments: Dict[str, DeploymentRecord] = {}
        self._deployment_lock = asyncio.Lock()

        # Performance tracking
        self._performance: Dict[str, Dict[str, float]] = {}

        # Callbacks
        self._on_deployment_complete: List[Callable[[DeploymentResult], None]] = []
        self._on_rollback: List[Callable[[str, str], None]] = []

        logger.info(
            f"[VoiceModelDeployer] Initialized with models_dir={self.models_dir}"
        )

    async def deploy_model(
        self,
        model_path: Path,
        model_type: ModelType,
        strategy: DeploymentStrategy = DeploymentStrategy.IMMEDIATE,
        version_id: Optional[str] = None,
        metrics: Optional[Dict[str, float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        traffic_percentage: float = 100.0,
    ) -> DeploymentResult:
        """
        Deploy a new voice model.

        Args:
            model_path: Path to model file(s)
            model_type: Type of model
            strategy: Deployment strategy
            version_id: Version identifier (auto-generated if None)
            metrics: Training metrics
            metadata: Additional metadata
            traffic_percentage: Initial traffic percentage (for gradual/AB)

        Returns:
            DeploymentResult with deployment status
        """
        start_time = time.perf_counter()

        # Generate version ID if not provided
        if version_id is None:
            version_id = f"v{int(time.time())}"

        # Get previous active version
        previous_version = self._registry.get_active_version(model_type)
        previous_version_id = previous_version.version_id if previous_version else None

        # Create deployment record
        deployment_id = f"deploy_{version_id}_{int(time.time())}"

        try:
            # Validate model
            if not model_path.exists():
                raise ValueError(f"Model path does not exist: {model_path}")

            # Calculate checksum
            checksum = self._calculate_checksum(model_path)

            # Copy model to models directory
            dest_path = self.models_dir / model_type.value / version_id
            dest_path.mkdir(parents=True, exist_ok=True)

            if model_path.is_dir():
                shutil.copytree(model_path, dest_path, dirs_exist_ok=True)
            else:
                shutil.copy2(model_path, dest_path / model_path.name)

            # Create version record
            version = ModelVersion(
                version_id=version_id,
                model_type=model_type,
                model_path=dest_path,
                created_at=time.time(),
                metrics=metrics or {},
                metadata=metadata or {},
                checksum=checksum,
            )

            # Register version
            await self._registry.register_version(version)

            # Create deployment record
            record = DeploymentRecord(
                deployment_id=deployment_id,
                version=version,
                strategy=strategy,
                status=DeploymentStatus.IN_PROGRESS,
                started_at=time.time(),
                traffic_percentage=traffic_percentage,
            )

            async with self._deployment_lock:
                self._deployments[deployment_id] = record

            # Execute deployment based on strategy
            if strategy == DeploymentStrategy.IMMEDIATE:
                await self._deploy_immediate(record)
            elif strategy == DeploymentStrategy.GRADUAL:
                await self._deploy_gradual(record)
            elif strategy == DeploymentStrategy.AB_TEST:
                await self._deploy_ab_test(record)
            elif strategy == DeploymentStrategy.SHADOW:
                await self._deploy_shadow(record)
            elif strategy == DeploymentStrategy.CANARY:
                await self._deploy_canary(record)

            record.status = DeploymentStatus.ACTIVE
            record.completed_at = time.time()

            elapsed = time.perf_counter() - start_time

            result = DeploymentResult(
                success=True,
                deployment_id=deployment_id,
                version_id=version_id,
                strategy=strategy,
                elapsed_seconds=elapsed,
                previous_version=previous_version_id,
            )

            # Notify callbacks
            for callback in self._on_deployment_complete:
                try:
                    callback(result)
                except Exception as e:
                    logger.debug(f"[VoiceModelDeployer] Callback error: {e}")

            logger.info(
                f"[VoiceModelDeployer] Deployed {model_type.value} v{version_id} "
                f"({strategy.value}) in {elapsed:.2f}s"
            )

            return result

        except Exception as e:
            elapsed = time.perf_counter() - start_time

            logger.error(
                f"[VoiceModelDeployer] Deployment failed for {model_type.value}: {e}"
            )

            return DeploymentResult(
                success=False,
                deployment_id=deployment_id,
                version_id=version_id,
                strategy=strategy,
                elapsed_seconds=elapsed,
                previous_version=previous_version_id,
                error=str(e),
            )

    async def _deploy_immediate(self, record: DeploymentRecord) -> None:
        """Immediate deployment - replace current model."""
        await self._registry.set_active(
            record.version.model_type,
            record.version.version_id,
        )
        record.traffic_percentage = 100.0

    async def _deploy_gradual(self, record: DeploymentRecord) -> None:
        """Gradual deployment - increase traffic over time."""
        # Start with initial traffic percentage
        await self._registry.set_active(
            record.version.model_type,
            record.version.version_id,
        )

        # Schedule gradual increase (handled by AB testing framework)
        logger.info(
            f"[VoiceModelDeployer] Gradual deployment started at "
            f"{record.traffic_percentage}% traffic"
        )

    async def _deploy_ab_test(self, record: DeploymentRecord) -> None:
        """A/B test deployment - controlled experiment."""
        # Register with AB testing framework
        try:
            from backend.voice_unlock.testing.ab_testing import (
                get_ab_testing_manager,
            )

            manager = await get_ab_testing_manager()
            await manager.start_test(
                test_id=record.deployment_id,
                control_version=self._registry.get_active_version(
                    record.version.model_type
                ).version_id if self._registry.get_active_version(record.version.model_type) else None,
                treatment_version=record.version.version_id,
                traffic_split=record.traffic_percentage / 100.0,
            )

        except ImportError:
            # Fall back to immediate deployment
            logger.warning(
                "[VoiceModelDeployer] AB testing not available, using immediate"
            )
            await self._deploy_immediate(record)

    async def _deploy_shadow(self, record: DeploymentRecord) -> None:
        """Shadow deployment - run in parallel without affecting decisions."""
        logger.info(
            f"[VoiceModelDeployer] Shadow deployment: {record.version.version_id} "
            "will run in parallel for monitoring"
        )
        # Don't change active version, just register for shadow execution

    async def _deploy_canary(self, record: DeploymentRecord) -> None:
        """Canary deployment - small percentage first."""
        record.traffic_percentage = 5.0  # Start with 5%
        await self._deploy_ab_test(record)

    def _calculate_checksum(self, path: Path) -> str:
        """Calculate checksum for model files."""
        hasher = hashlib.sha256()

        if path.is_file():
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)
        else:
            # Hash all files in directory
            for file_path in sorted(path.rglob("*")):
                if file_path.is_file():
                    with open(file_path, "rb") as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            hasher.update(chunk)

        return hasher.hexdigest()[:16]

    async def rollback(
        self,
        model_type: ModelType,
        to_version: Optional[str] = None,
    ) -> bool:
        """
        Rollback to a previous model version.

        Args:
            model_type: Type of model to rollback
            to_version: Version to rollback to (previous if None)

        Returns:
            True if rollback successful
        """
        try:
            current = self._registry.get_active_version(model_type)
            if not current:
                logger.warning(f"[VoiceModelDeployer] No active version to rollback")
                return False

            # Get target version
            if to_version:
                target = self._registry.get_version(model_type, to_version)
            else:
                # Get previous version
                versions = self._registry.get_all_versions(model_type)
                target = versions[1] if len(versions) > 1 else None

            if not target:
                logger.warning(f"[VoiceModelDeployer] No previous version to rollback to")
                return False

            # Perform rollback
            await self._registry.set_active(model_type, target.version_id)

            # Update deployment records
            for record in self._deployments.values():
                if (record.version.model_type == model_type and
                    record.version.version_id == current.version_id):
                    record.status = DeploymentStatus.ROLLED_BACK

            # Notify callbacks
            for callback in self._on_rollback:
                try:
                    callback(current.version_id, target.version_id)
                except Exception:
                    pass

            logger.info(
                f"[VoiceModelDeployer] Rolled back {model_type.value}: "
                f"v{current.version_id} → v{target.version_id}"
            )

            return True

        except Exception as e:
            logger.error(f"[VoiceModelDeployer] Rollback failed: {e}")
            return False

    def get_active_version(self, model_type: ModelType) -> Optional[ModelVersion]:
        """Get the currently active version for a model type."""
        return self._registry.get_active_version(model_type)

    def get_deployment_history(
        self,
        model_type: Optional[ModelType] = None,
    ) -> List[DeploymentRecord]:
        """Get deployment history."""
        records = list(self._deployments.values())

        if model_type:
            records = [r for r in records if r.version.model_type == model_type]

        return sorted(records, key=lambda r: r.started_at, reverse=True)

    def on_deployment_complete(
        self,
        callback: Callable[[DeploymentResult], None],
    ) -> None:
        """Register callback for deployment completion."""
        self._on_deployment_complete.append(callback)

    def on_rollback(
        self,
        callback: Callable[[str, str], None],
    ) -> None:
        """Register callback for rollback events."""
        self._on_rollback.append(callback)


# =============================================================================
# Singleton Access
# =============================================================================

_deployer_instance: Optional[VoiceModelDeployer] = None
_deployer_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_voice_model_deployer() -> VoiceModelDeployer:
    """Get the singleton model deployer."""
    global _deployer_instance

    async with _deployer_lock:
        if _deployer_instance is None:
            _deployer_instance = VoiceModelDeployer()
        return _deployer_instance


async def deploy_voice_model(
    model_path: Path,
    model_type: ModelType,
    strategy: DeploymentStrategy = DeploymentStrategy.IMMEDIATE,
    **kwargs,
) -> DeploymentResult:
    """Convenience function to deploy a model."""
    deployer = await get_voice_model_deployer()
    return await deployer.deploy_model(
        model_path=model_path,
        model_type=model_type,
        strategy=strategy,
        **kwargs,
    )
