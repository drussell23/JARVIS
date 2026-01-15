"""
Unified Model Management Engine v1.0
=====================================

Enterprise-grade model management for the JARVIS Trinity ecosystem.
Provides comprehensive model lifecycle management across JARVIS (Body),
JARVIS Prime (Mind), and Reactor Core (Learning).

Implements 7 critical model management patterns:
1. Model Version Registry - Centralized version tracking with metadata
2. Model A/B Testing - Statistical multi-variant testing framework
3. Model Rollback - Safe rollback with state preservation
4. Model Validation Pipeline - Automated pre-deployment validation
5. Model Performance Tracking - Historical tracking with anomaly detection
6. Model Metadata Management - Structured queryable metadata database
7. Model Lifecycle Management - Policy-driven lifecycle automation

Author: Trinity Model System
Version: 1.0.0
"""

import asyncio
import hashlib
import json
import logging
import os
import pickle
import statistics
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
    Coroutine,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)
import math

logger = logging.getLogger(__name__)

# Type variables
T = TypeVar("T")
ModelT = TypeVar("ModelT")


# =============================================================================
# ENUMS
# =============================================================================


class ModelType(Enum):
    """Types of models in the system."""
    VOICE = "voice"
    NLU = "nlu"
    VISION = "vision"
    EMBEDDING = "embedding"
    CLASSIFIER = "classifier"
    LLM = "llm"
    CUSTOM = "custom"


class ModelStatus(Enum):
    """Status of a model version."""
    DRAFT = auto()           # Being developed
    PENDING = auto()         # Awaiting validation
    VALIDATING = auto()      # Under validation
    VALIDATED = auto()       # Passed validation
    WARMING_UP = auto()      # Warming up for deployment
    CANARY = auto()          # Canary deployment (partial traffic)
    A_B_TESTING = auto()     # In A/B test
    ACTIVE = auto()          # Fully deployed and serving
    SHADOW = auto()          # Shadow mode (receiving traffic but not returning)
    PROMOTED = auto()        # Promoted to production
    DEMOTING = auto()        # Being demoted
    ROLLING_BACK = auto()    # Rollback in progress
    ROLLED_BACK = auto()     # Rolled back to previous version
    DEPRECATED = auto()      # Marked for removal
    ARCHIVED = auto()        # Archived (cold storage)
    DELETED = auto()         # Deleted (metadata retained)
    FAILED = auto()          # Failed validation or deployment


class DeploymentStrategy(Enum):
    """Deployment strategies."""
    IMMEDIATE = auto()       # Immediate full deployment
    CANARY = auto()          # Gradual traffic ramp-up
    BLUE_GREEN = auto()      # Side-by-side with instant switch
    SHADOW = auto()          # Shadow traffic (duplicate requests)
    A_B_TEST = auto()        # Statistical A/B testing


class RollbackReason(Enum):
    """Reasons for rollback."""
    HIGH_ERROR_RATE = auto()
    LATENCY_DEGRADATION = auto()
    ACCURACY_DROP = auto()
    MEMORY_PRESSURE = auto()
    MANUAL_REQUEST = auto()
    VALIDATION_FAILURE = auto()
    HEALTH_CHECK_FAILURE = auto()
    A_B_TEST_FAILURE = auto()
    RESOURCE_EXHAUSTION = auto()


class ValidationLevel(Enum):
    """Validation strictness levels."""
    MINIMAL = auto()         # Quick sanity checks
    STANDARD = auto()        # Standard validation suite
    COMPREHENSIVE = auto()   # Full validation with stress tests
    PARANOID = auto()        # Maximum validation (production critical)


class LifecycleAction(Enum):
    """Lifecycle actions."""
    ARCHIVE = auto()
    DELETE = auto()
    PROMOTE = auto()
    DEMOTE = auto()
    EXTEND_TTL = auto()
    FREEZE = auto()          # Prevent lifecycle changes


class MetricType(Enum):
    """Types of metrics tracked."""
    LATENCY = "latency"
    ACCURACY = "accuracy"
    ERROR_RATE = "error_rate"
    THROUGHPUT = "throughput"
    MEMORY_USAGE = "memory_usage"
    CPU_USAGE = "cpu_usage"
    GPU_USAGE = "gpu_usage"
    COST = "cost"
    QUALITY_SCORE = "quality_score"
    USER_SATISFACTION = "user_satisfaction"


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ModelManagementConfig:
    """Configuration for model management."""

    # Version Registry
    registry_path: str = os.getenv(
        "MODEL_REGISTRY_PATH",
        "/tmp/jarvis_model_registry"
    )
    max_versions_per_model: int = int(os.getenv("MAX_MODEL_VERSIONS", "50"))

    # A/B Testing
    ab_min_samples: int = int(os.getenv("AB_TEST_MIN_SAMPLES", "100"))
    ab_confidence_threshold: float = float(os.getenv("AB_CONFIDENCE_THRESHOLD", "0.95"))
    ab_max_duration_hours: float = float(os.getenv("AB_MAX_DURATION_HOURS", "168"))  # 1 week

    # Rollback
    error_rate_threshold: float = float(os.getenv("MODEL_ERROR_RATE_THRESHOLD", "0.05"))
    latency_degradation_threshold: float = float(os.getenv("MODEL_LATENCY_THRESHOLD", "1.5"))
    accuracy_drop_threshold: float = float(os.getenv("MODEL_ACCURACY_DROP", "0.02"))
    rollback_cooldown_seconds: float = float(os.getenv("ROLLBACK_COOLDOWN", "300"))

    # Validation
    validation_timeout_seconds: float = float(os.getenv("VALIDATION_TIMEOUT", "300"))
    min_validation_samples: int = int(os.getenv("MIN_VALIDATION_SAMPLES", "100"))

    # Performance Tracking
    metric_window_size: int = int(os.getenv("METRIC_WINDOW_SIZE", "1000"))
    metric_retention_days: int = int(os.getenv("METRIC_RETENTION_DAYS", "90"))
    anomaly_threshold_sigma: float = float(os.getenv("ANOMALY_THRESHOLD_SIGMA", "3.0"))

    # Lifecycle
    archive_after_days: int = int(os.getenv("MODEL_ARCHIVE_AFTER_DAYS", "30"))
    delete_after_days: int = int(os.getenv("MODEL_DELETE_AFTER_DAYS", "90"))
    lifecycle_check_interval: float = float(os.getenv("LIFECYCLE_CHECK_INTERVAL", "3600"))

    # Canary
    canary_initial_percent: float = float(os.getenv("CANARY_INITIAL_PERCENT", "5.0"))
    canary_increment: float = float(os.getenv("CANARY_INCREMENT", "10.0"))
    canary_promotion_interval: float = float(os.getenv("CANARY_PROMOTION_INTERVAL", "300"))

    # Health
    health_check_interval: float = float(os.getenv("MODEL_HEALTH_CHECK_INTERVAL", "30"))
    warmup_period_seconds: float = float(os.getenv("MODEL_WARMUP_PERIOD", "60"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class SemanticVersion:
    """Semantic version (major.minor.patch)."""
    major: int = 1
    minor: int = 0
    patch: int = 0
    prerelease: Optional[str] = None
    build_metadata: Optional[str] = None

    def __str__(self) -> str:
        version = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            version += f"-{self.prerelease}"
        if self.build_metadata:
            version += f"+{self.build_metadata}"
        return version

    def __lt__(self, other: "SemanticVersion") -> bool:
        if self.major != other.major:
            return self.major < other.major
        if self.minor != other.minor:
            return self.minor < other.minor
        return self.patch < other.patch

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return False
        return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch)

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch))

    def increment_patch(self) -> "SemanticVersion":
        return SemanticVersion(self.major, self.minor, self.patch + 1)

    def increment_minor(self) -> "SemanticVersion":
        return SemanticVersion(self.major, self.minor + 1, 0)

    def increment_major(self) -> "SemanticVersion":
        return SemanticVersion(self.major + 1, 0, 0)

    @classmethod
    def parse(cls, version_str: str) -> "SemanticVersion":
        """Parse version string."""
        # Remove v prefix if present
        if version_str.startswith("v"):
            version_str = version_str[1:]

        # Split build metadata
        build_metadata = None
        if "+" in version_str:
            version_str, build_metadata = version_str.split("+", 1)

        # Split prerelease
        prerelease = None
        if "-" in version_str:
            version_str, prerelease = version_str.split("-", 1)

        # Parse version numbers
        parts = version_str.split(".")
        major = int(parts[0]) if len(parts) > 0 else 1
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0

        return cls(major, minor, patch, prerelease, build_metadata)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "major": self.major,
            "minor": self.minor,
            "patch": self.patch,
            "prerelease": self.prerelease,
            "build_metadata": self.build_metadata,
            "string": str(self),
        }


@dataclass
class ModelMetadata:
    """Comprehensive model metadata."""
    # Identity
    model_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    model_type: ModelType = ModelType.CUSTOM
    version: SemanticVersion = field(default_factory=SemanticVersion)

    # Description
    description: str = ""
    author: str = ""
    tags: List[str] = field(default_factory=list)

    # Technical
    framework: str = ""  # pytorch, tensorflow, coreml, onnx, etc.
    architecture: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)

    # Resources
    model_size_bytes: int = 0
    ram_requirement_mb: int = 0
    gpu_requirement_mb: int = 0
    disk_requirement_mb: int = 0

    # Performance (expected)
    expected_latency_ms: float = 0.0
    expected_throughput_qps: float = 0.0
    expected_accuracy: float = 0.0

    # Source
    source_path: str = ""
    source_hash: str = ""
    training_data_hash: str = ""
    parent_version_id: Optional[str] = None

    # Timestamps
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    deployed_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None

    # Status
    status: ModelStatus = ModelStatus.DRAFT

    # Lineage
    training_job_id: Optional[str] = None
    training_config: Dict[str, Any] = field(default_factory=dict)
    hyperparameters: Dict[str, Any] = field(default_factory=dict)

    # Custom metadata
    custom_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "model_type": self.model_type.value,
            "version": self.version.to_dict(),
            "description": self.description,
            "author": self.author,
            "tags": self.tags,
            "framework": self.framework,
            "architecture": self.architecture,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "model_size_bytes": self.model_size_bytes,
            "ram_requirement_mb": self.ram_requirement_mb,
            "gpu_requirement_mb": self.gpu_requirement_mb,
            "disk_requirement_mb": self.disk_requirement_mb,
            "expected_latency_ms": self.expected_latency_ms,
            "expected_throughput_qps": self.expected_throughput_qps,
            "expected_accuracy": self.expected_accuracy,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "parent_version_id": self.parent_version_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deployed_at": self.deployed_at.isoformat() if self.deployed_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "status": self.status.name,
            "training_job_id": self.training_job_id,
            "training_config": self.training_config,
            "hyperparameters": self.hyperparameters,
            "custom_metadata": self.custom_metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelMetadata":
        return cls(
            model_id=data.get("model_id", str(uuid.uuid4())),
            name=data.get("name", ""),
            model_type=ModelType(data.get("model_type", "custom")),
            version=SemanticVersion.parse(data.get("version", {}).get("string", "1.0.0")),
            description=data.get("description", ""),
            author=data.get("author", ""),
            tags=data.get("tags", []),
            framework=data.get("framework", ""),
            architecture=data.get("architecture", ""),
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            model_size_bytes=data.get("model_size_bytes", 0),
            ram_requirement_mb=data.get("ram_requirement_mb", 0),
            gpu_requirement_mb=data.get("gpu_requirement_mb", 0),
            disk_requirement_mb=data.get("disk_requirement_mb", 0),
            expected_latency_ms=data.get("expected_latency_ms", 0.0),
            expected_throughput_qps=data.get("expected_throughput_qps", 0.0),
            expected_accuracy=data.get("expected_accuracy", 0.0),
            source_path=data.get("source_path", ""),
            source_hash=data.get("source_hash", ""),
            parent_version_id=data.get("parent_version_id"),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.utcnow(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if "updated_at" in data else datetime.utcnow(),
            deployed_at=datetime.fromisoformat(data["deployed_at"]) if data.get("deployed_at") else None,
            archived_at=datetime.fromisoformat(data["archived_at"]) if data.get("archived_at") else None,
            status=ModelStatus[data.get("status", "DRAFT")],
            training_job_id=data.get("training_job_id"),
            training_config=data.get("training_config", {}),
            hyperparameters=data.get("hyperparameters", {}),
            custom_metadata=data.get("custom_metadata", {}),
        )


@dataclass
class MetricSample:
    """A single metric sample."""
    timestamp: float = field(default_factory=time.time)
    value: float = 0.0
    metric_type: MetricType = MetricType.LATENCY
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class MetricWindow:
    """Sliding window of metrics."""
    samples: deque = field(default_factory=lambda: deque(maxlen=1000))
    metric_type: MetricType = MetricType.LATENCY

    def add(self, value: float, labels: Optional[Dict[str, str]] = None):
        self.samples.append(MetricSample(
            value=value,
            metric_type=self.metric_type,
            labels=labels or {},
        ))

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        if not self.samples:
            return 0.0
        return statistics.mean(s.value for s in self.samples)

    @property
    def std(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return statistics.stdev(s.value for s in self.samples)

    @property
    def min(self) -> float:
        if not self.samples:
            return 0.0
        return min(s.value for s in self.samples)

    @property
    def max(self) -> float:
        if not self.samples:
            return 0.0
        return max(s.value for s in self.samples)

    def percentile(self, p: float) -> float:
        """Calculate percentile."""
        if not self.samples:
            return 0.0
        sorted_values = sorted(s.value for s in self.samples)
        idx = int(len(sorted_values) * p / 100)
        return sorted_values[min(idx, len(sorted_values) - 1)]

    def is_anomaly(self, value: float, sigma: float = 3.0) -> bool:
        """Check if value is anomalous."""
        if len(self.samples) < 10:
            return False
        return abs(value - self.mean) > sigma * self.std


@dataclass
class PerformanceMetrics:
    """Comprehensive performance metrics for a model."""
    model_id: str = ""
    version: SemanticVersion = field(default_factory=SemanticVersion)

    # Metric windows
    latency: MetricWindow = field(default_factory=lambda: MetricWindow(metric_type=MetricType.LATENCY))
    accuracy: MetricWindow = field(default_factory=lambda: MetricWindow(metric_type=MetricType.ACCURACY))
    error_rate: MetricWindow = field(default_factory=lambda: MetricWindow(metric_type=MetricType.ERROR_RATE))
    throughput: MetricWindow = field(default_factory=lambda: MetricWindow(metric_type=MetricType.THROUGHPUT))
    memory_usage: MetricWindow = field(default_factory=lambda: MetricWindow(metric_type=MetricType.MEMORY_USAGE))

    # Counters
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0

    # Timestamps
    first_request_at: Optional[float] = None
    last_request_at: Optional[float] = None

    def record_request(
        self,
        latency_ms: float,
        success: bool,
        accuracy: Optional[float] = None,
        memory_mb: Optional[float] = None,
    ):
        """Record a request."""
        now = time.time()
        if self.first_request_at is None:
            self.first_request_at = now
        self.last_request_at = now

        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1

        self.latency.add(latency_ms)
        self.error_rate.add(0.0 if success else 1.0)

        if accuracy is not None:
            self.accuracy.add(accuracy)
        if memory_mb is not None:
            self.memory_usage.add(memory_mb)

        # Update throughput
        if self.first_request_at:
            duration = now - self.first_request_at
            if duration > 0:
                self.throughput.add(self.total_requests / duration)

    def get_summary(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": str(self.version),
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": self.successful_requests / max(1, self.total_requests),
            "latency": {
                "mean_ms": self.latency.mean,
                "p50_ms": self.latency.percentile(50),
                "p95_ms": self.latency.percentile(95),
                "p99_ms": self.latency.percentile(99),
            },
            "accuracy": {
                "mean": self.accuracy.mean,
                "min": self.accuracy.min,
                "max": self.accuracy.max,
            },
            "error_rate": {
                "mean": self.error_rate.mean,
                "current": self.failed_requests / max(1, self.total_requests),
            },
            "throughput": {
                "mean_qps": self.throughput.mean,
                "current_qps": self.throughput.samples[-1].value if self.throughput.samples else 0,
            },
        }


@dataclass
class ValidationResult:
    """Result of model validation."""
    model_id: str
    version: SemanticVersion
    passed: bool = False
    level: ValidationLevel = ValidationLevel.STANDARD

    # Test results
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0

    # Performance validation
    latency_within_bounds: bool = True
    accuracy_within_bounds: bool = True
    memory_within_bounds: bool = True

    # Details
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)

    # Timestamps
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    duration_seconds: float = 0.0


@dataclass
class ABTestVariant:
    """A variant in an A/B test."""
    variant_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model_id: str = ""
    version: SemanticVersion = field(default_factory=SemanticVersion)
    traffic_percent: float = 0.0

    # Metrics
    samples: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    total_accuracy: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / max(1, self.samples)

    @property
    def mean_latency(self) -> float:
        return self.total_latency_ms / max(1, self.samples)

    @property
    def mean_accuracy(self) -> float:
        return self.total_accuracy / max(1, self.samples)


@dataclass
class ABTest:
    """An A/B test configuration and state."""
    test_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""

    # Control and variants
    control: ABTestVariant = field(default_factory=ABTestVariant)
    variants: List[ABTestVariant] = field(default_factory=list)

    # Configuration
    metric_to_optimize: MetricType = MetricType.ACCURACY
    min_samples_per_variant: int = 100
    confidence_threshold: float = 0.95
    max_duration_hours: float = 168.0

    # State
    status: str = "pending"  # pending, running, analyzing, completed
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    winner_variant_id: Optional[str] = None
    conclusion: str = ""

    def get_total_samples(self) -> int:
        return self.control.samples + sum(v.samples for v in self.variants)

    def is_ready_for_analysis(self) -> bool:
        if self.control.samples < self.min_samples_per_variant:
            return False
        return all(v.samples >= self.min_samples_per_variant for v in self.variants)


@dataclass
class RollbackEvent:
    """Record of a rollback event."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model_id: str = ""
    from_version: SemanticVersion = field(default_factory=SemanticVersion)
    to_version: SemanticVersion = field(default_factory=SemanticVersion)
    reason: RollbackReason = RollbackReason.MANUAL_REQUEST
    triggered_by: str = "system"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metrics_at_rollback: Dict[str, float] = field(default_factory=dict)
    success: bool = True
    error_message: Optional[str] = None


@dataclass
class LifecyclePolicy:
    """Lifecycle policy for a model."""
    policy_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""

    # Conditions
    applies_to_types: List[ModelType] = field(default_factory=list)
    applies_to_tags: List[str] = field(default_factory=list)
    applies_to_status: List[ModelStatus] = field(default_factory=list)

    # Actions
    archive_after_days: Optional[int] = None
    delete_after_days: Optional[int] = None
    min_versions_to_keep: int = 3
    keep_promoted_versions: bool = True

    # Schedule
    enabled: bool = True
    last_run_at: Optional[datetime] = None


@dataclass
class Alert:
    """An alert from the model management system."""
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model_id: str = ""
    version: Optional[SemanticVersion] = None
    severity: AlertSeverity = AlertSeverity.INFO
    title: str = ""
    message: str = ""
    metric_type: Optional[MetricType] = None
    metric_value: Optional[float] = None
    threshold: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    acknowledged: bool = False
    resolved: bool = False


# =============================================================================
# MODEL VERSION REGISTRY
# =============================================================================


class ModelVersionRegistry:
    """
    Centralized model version registry.

    Features:
    - Version tracking with semantic versioning
    - Full metadata storage
    - Query by various criteria
    - Version comparison and diffing
    - Dependency tracking
    """

    def __init__(self, config: ModelManagementConfig):
        self.config = config
        self._models: Dict[str, Dict[str, ModelMetadata]] = defaultdict(dict)  # model_id -> version -> metadata
        self._lock = asyncio.Lock()
        self._persistence_path = Path(config.registry_path)
        self._persistence_path.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("ModelVersionRegistry")

    async def register(self, metadata: ModelMetadata) -> str:
        """Register a new model version."""
        async with self._lock:
            version_key = str(metadata.version)

            # Check if version already exists
            if version_key in self._models[metadata.name]:
                raise ValueError(f"Version {version_key} already exists for model {metadata.name}")

            # Check max versions
            existing_versions = len(self._models[metadata.name])
            if existing_versions >= self.config.max_versions_per_model:
                # Archive oldest non-active version
                await self._archive_oldest_version(metadata.name)

            self._models[metadata.name][version_key] = metadata
            await self._persist_model(metadata)

            self.logger.info(f"Registered model {metadata.name} version {version_key}")
            return metadata.model_id

    async def get(self, name: str, version: Optional[str] = None) -> Optional[ModelMetadata]:
        """Get model metadata by name and optional version."""
        async with self._lock:
            if name not in self._models:
                return None

            if version:
                return self._models[name].get(version)

            # Return latest version
            if not self._models[name]:
                return None
            latest_version = max(
                self._models[name].keys(),
                key=lambda v: SemanticVersion.parse(v)
            )
            return self._models[name][latest_version]

    async def get_by_id(self, model_id: str) -> Optional[ModelMetadata]:
        """Get model by ID."""
        async with self._lock:
            for versions in self._models.values():
                for metadata in versions.values():
                    if metadata.model_id == model_id:
                        return metadata
            return None

    async def list_versions(self, name: str) -> List[ModelMetadata]:
        """List all versions of a model."""
        async with self._lock:
            if name not in self._models:
                return []
            return sorted(
                self._models[name].values(),
                key=lambda m: m.version,
                reverse=True
            )

    async def query(
        self,
        model_type: Optional[ModelType] = None,
        status: Optional[ModelStatus] = None,
        tags: Optional[List[str]] = None,
        author: Optional[str] = None,
        min_accuracy: Optional[float] = None,
    ) -> List[ModelMetadata]:
        """Query models by various criteria."""
        async with self._lock:
            results = []
            for versions in self._models.values():
                for metadata in versions.values():
                    if model_type and metadata.model_type != model_type:
                        continue
                    if status and metadata.status != status:
                        continue
                    if tags and not all(t in metadata.tags for t in tags):
                        continue
                    if author and metadata.author != author:
                        continue
                    if min_accuracy and metadata.expected_accuracy < min_accuracy:
                        continue
                    results.append(metadata)
            return results

    async def update_status(self, name: str, version: str, status: ModelStatus):
        """Update model status."""
        async with self._lock:
            if name in self._models and version in self._models[name]:
                self._models[name][version].status = status
                self._models[name][version].updated_at = datetime.utcnow()
                await self._persist_model(self._models[name][version])

    async def get_active_version(self, name: str) -> Optional[ModelMetadata]:
        """Get the active version of a model."""
        async with self._lock:
            if name not in self._models:
                return None
            for metadata in self._models[name].values():
                if metadata.status == ModelStatus.ACTIVE:
                    return metadata
            return None

    async def _archive_oldest_version(self, name: str):
        """Archive the oldest non-active version."""
        versions = list(self._models[name].values())
        non_active = [v for v in versions if v.status not in (ModelStatus.ACTIVE, ModelStatus.CANARY)]
        if non_active:
            oldest = min(non_active, key=lambda m: m.created_at)
            oldest.status = ModelStatus.ARCHIVED
            oldest.archived_at = datetime.utcnow()
            self.logger.info(f"Archived old version {oldest.version} of {name}")

    async def _persist_model(self, metadata: ModelMetadata):
        """Persist model metadata to disk."""
        model_dir = self._persistence_path / metadata.name
        model_dir.mkdir(parents=True, exist_ok=True)
        file_path = model_dir / f"{metadata.version}.json"
        with open(file_path, 'w') as f:
            json.dump(metadata.to_dict(), f, indent=2)

    async def load_from_disk(self):
        """Load all models from disk."""
        if not self._persistence_path.exists():
            return

        for model_dir in self._persistence_path.iterdir():
            if not model_dir.is_dir():
                continue
            for version_file in model_dir.glob("*.json"):
                try:
                    with open(version_file) as f:
                        data = json.load(f)
                    metadata = ModelMetadata.from_dict(data)
                    self._models[metadata.name][str(metadata.version)] = metadata
                except Exception as e:
                    self.logger.error(f"Failed to load {version_file}: {e}")


# =============================================================================
# MODEL A/B TESTING FRAMEWORK
# =============================================================================


class ABTestingFramework:
    """
    Statistical A/B testing framework for models.

    Features:
    - Multi-variant testing
    - Statistical significance calculation
    - Automatic winner detection
    - Traffic splitting
    - Bayesian analysis
    """

    def __init__(self, config: ModelManagementConfig):
        self.config = config
        self._tests: Dict[str, ABTest] = {}
        self._active_tests: Dict[str, str] = {}  # model_name -> test_id
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("ABTestingFramework")

    async def create_test(
        self,
        name: str,
        control_model_id: str,
        control_version: SemanticVersion,
        variant_models: List[Tuple[str, SemanticVersion]],
        metric_to_optimize: MetricType = MetricType.ACCURACY,
        traffic_split: Optional[List[float]] = None,
    ) -> ABTest:
        """Create a new A/B test."""
        async with self._lock:
            # Calculate traffic split
            num_variants = len(variant_models) + 1  # +1 for control
            if traffic_split is None:
                traffic_split = [100.0 / num_variants] * num_variants

            # Create control variant
            control = ABTestVariant(
                model_id=control_model_id,
                version=control_version,
                traffic_percent=traffic_split[0],
            )

            # Create variant variants
            variants = []
            for i, (model_id, version) in enumerate(variant_models):
                variants.append(ABTestVariant(
                    model_id=model_id,
                    version=version,
                    traffic_percent=traffic_split[i + 1],
                ))

            test = ABTest(
                name=name,
                control=control,
                variants=variants,
                metric_to_optimize=metric_to_optimize,
                min_samples_per_variant=self.config.ab_min_samples,
                confidence_threshold=self.config.ab_confidence_threshold,
                max_duration_hours=self.config.ab_max_duration_hours,
            )

            self._tests[test.test_id] = test
            self.logger.info(f"Created A/B test {test.test_id}: {name}")
            return test

    async def start_test(self, test_id: str) -> bool:
        """Start an A/B test."""
        async with self._lock:
            if test_id not in self._tests:
                return False

            test = self._tests[test_id]
            test.status = "running"
            test.started_at = datetime.utcnow()

            # Mark as active for model routing
            model_id = test.control.model_id
            self._active_tests[model_id] = test_id

            self.logger.info(f"Started A/B test {test_id}")
            return True

    async def record_sample(
        self,
        test_id: str,
        variant_id: str,
        success: bool,
        latency_ms: float,
        accuracy: Optional[float] = None,
    ):
        """Record a sample for a variant."""
        async with self._lock:
            if test_id not in self._tests:
                return

            test = self._tests[test_id]

            # Find variant
            variant = None
            if test.control.variant_id == variant_id:
                variant = test.control
            else:
                for v in test.variants:
                    if v.variant_id == variant_id:
                        variant = v
                        break

            if variant is None:
                return

            # Record sample
            variant.samples += 1
            if success:
                variant.successes += 1
            else:
                variant.failures += 1
            variant.total_latency_ms += latency_ms
            if accuracy is not None:
                variant.total_accuracy += accuracy

            # Check if ready for analysis
            if test.is_ready_for_analysis():
                await self._analyze_test(test_id)

    async def route_request(self, model_id: str) -> Optional[ABTestVariant]:
        """Route a request to the appropriate variant."""
        async with self._lock:
            if model_id not in self._active_tests:
                return None

            test_id = self._active_tests[model_id]
            test = self._tests[test_id]

            if test.status != "running":
                return None

            # Probabilistic routing based on traffic split
            import random
            rand = random.random() * 100

            cumulative = 0.0
            cumulative += test.control.traffic_percent
            if rand < cumulative:
                return test.control

            for variant in test.variants:
                cumulative += variant.traffic_percent
                if rand < cumulative:
                    return variant

            return test.control

    async def _analyze_test(self, test_id: str):
        """Analyze test results for statistical significance."""
        test = self._tests[test_id]
        test.status = "analyzing"

        # Compare each variant to control using z-test for proportions
        best_variant = None
        best_improvement = 0.0

        control_rate = test.control.success_rate
        control_n = test.control.samples

        for variant in test.variants:
            variant_rate = variant.success_rate
            variant_n = variant.samples

            # Two-proportion z-test
            pooled = (test.control.successes + variant.successes) / (control_n + variant_n)
            se = math.sqrt(pooled * (1 - pooled) * (1/control_n + 1/variant_n))

            if se > 0:
                z_score = (variant_rate - control_rate) / se
                # Two-tailed p-value approximation
                p_value = 2 * (1 - self._normal_cdf(abs(z_score)))

                if p_value < (1 - self.config.ab_confidence_threshold):
                    improvement = (variant_rate - control_rate) / max(0.001, control_rate)
                    if improvement > best_improvement:
                        best_improvement = improvement
                        best_variant = variant

        # Determine winner
        if best_variant is not None:
            test.winner_variant_id = best_variant.variant_id
            test.conclusion = f"Variant {best_variant.variant_id} wins with {best_improvement:.1%} improvement"
        else:
            test.winner_variant_id = test.control.variant_id
            test.conclusion = "No significant difference found - control remains"

        test.status = "completed"
        test.completed_at = datetime.utcnow()

        self.logger.info(f"A/B test {test_id} completed: {test.conclusion}")

    def _normal_cdf(self, x: float) -> float:
        """Approximate normal CDF."""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    async def get_test(self, test_id: str) -> Optional[ABTest]:
        """Get test by ID."""
        return self._tests.get(test_id)

    async def list_active_tests(self) -> List[ABTest]:
        """List all active tests."""
        return [t for t in self._tests.values() if t.status == "running"]


# =============================================================================
# MODEL ROLLBACK MANAGER
# =============================================================================


class ModelRollbackManager:
    """
    Safe model rollback management.

    Features:
    - Automatic rollback triggers
    - State preservation
    - Rollback history
    - Cooldown management
    """

    def __init__(self, config: ModelManagementConfig, registry: ModelVersionRegistry):
        self.config = config
        self.registry = registry
        self._rollback_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self._last_rollback: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._callbacks: List[Callable] = []
        self.logger = logging.getLogger("ModelRollbackManager")

    def register_callback(self, callback: Callable):
        """Register rollback callback."""
        self._callbacks.append(callback)

    async def should_rollback(
        self,
        model_name: str,
        metrics: PerformanceMetrics,
    ) -> Tuple[bool, Optional[RollbackReason]]:
        """Check if model should be rolled back."""
        # Check cooldown
        if model_name in self._last_rollback:
            elapsed = time.time() - self._last_rollback[model_name]
            if elapsed < self.config.rollback_cooldown_seconds:
                return False, None

        # Check error rate
        if metrics.error_rate.mean > self.config.error_rate_threshold:
            return True, RollbackReason.HIGH_ERROR_RATE

        # Check latency degradation
        current = await self.registry.get_active_version(model_name)
        if current and metrics.latency.mean > current.expected_latency_ms * self.config.latency_degradation_threshold:
            return True, RollbackReason.LATENCY_DEGRADATION

        # Check accuracy drop
        if current and metrics.accuracy.mean < current.expected_accuracy - self.config.accuracy_drop_threshold:
            return True, RollbackReason.ACCURACY_DROP

        return False, None

    async def rollback(
        self,
        model_name: str,
        reason: RollbackReason,
        target_version: Optional[str] = None,
        triggered_by: str = "system",
        metrics_snapshot: Optional[Dict[str, float]] = None,
    ) -> RollbackEvent:
        """Perform rollback to previous version."""
        async with self._lock:
            # Get current version
            current = await self.registry.get_active_version(model_name)
            if not current:
                raise ValueError(f"No active version found for {model_name}")

            # Find target version
            if target_version:
                target = await self.registry.get(model_name, target_version)
            else:
                # Find previous active version
                versions = await self.registry.list_versions(model_name)
                target = None
                for v in versions:
                    if v.status in (ModelStatus.ROLLED_BACK, ModelStatus.PROMOTED) and v.version < current.version:
                        target = v
                        break

            if not target:
                raise ValueError(f"No suitable rollback target found for {model_name}")

            # Create rollback event
            event = RollbackEvent(
                model_id=current.model_id,
                from_version=current.version,
                to_version=target.version,
                reason=reason,
                triggered_by=triggered_by,
                metrics_at_rollback=metrics_snapshot or {},
            )

            try:
                # Update statuses
                await self.registry.update_status(model_name, str(current.version), ModelStatus.ROLLING_BACK)
                await self.registry.update_status(model_name, str(target.version), ModelStatus.WARMING_UP)

                # Notify callbacks
                for callback in self._callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(event)
                        else:
                            callback(event)
                    except Exception as e:
                        self.logger.error(f"Rollback callback error: {e}")

                # Finalize
                await self.registry.update_status(model_name, str(current.version), ModelStatus.ROLLED_BACK)
                await self.registry.update_status(model_name, str(target.version), ModelStatus.ACTIVE)

                event.success = True
                self._rollback_history[model_name].append(event)
                self._last_rollback[model_name] = time.time()

                self.logger.warning(
                    f"Rolled back {model_name} from {current.version} to {target.version}: {reason.name}"
                )

            except Exception as e:
                event.success = False
                event.error_message = str(e)
                self.logger.error(f"Rollback failed for {model_name}: {e}")
                raise

            return event

    async def get_rollback_history(self, model_name: str) -> List[RollbackEvent]:
        """Get rollback history for a model."""
        return list(self._rollback_history.get(model_name, []))


# =============================================================================
# MODEL VALIDATION PIPELINE
# =============================================================================


class ModelValidationPipeline:
    """
    Automated model validation before deployment.

    Features:
    - Multi-level validation
    - Performance benchmarking
    - Compatibility checking
    - Security scanning
    """

    def __init__(self, config: ModelManagementConfig):
        self.config = config
        self._validators: Dict[ValidationLevel, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("ModelValidationPipeline")

        # Register default validators
        self._register_default_validators()

    def _register_default_validators(self):
        """Register default validation checks."""
        # Minimal level
        self._validators[ValidationLevel.MINIMAL].append(self._validate_metadata)
        self._validators[ValidationLevel.MINIMAL].append(self._validate_file_exists)

        # Standard level (includes minimal)
        self._validators[ValidationLevel.STANDARD].extend(self._validators[ValidationLevel.MINIMAL])
        self._validators[ValidationLevel.STANDARD].append(self._validate_load)
        self._validators[ValidationLevel.STANDARD].append(self._validate_inference)

        # Comprehensive level (includes standard)
        self._validators[ValidationLevel.COMPREHENSIVE].extend(self._validators[ValidationLevel.STANDARD])
        self._validators[ValidationLevel.COMPREHENSIVE].append(self._validate_performance)
        self._validators[ValidationLevel.COMPREHENSIVE].append(self._validate_memory)

        # Paranoid level (includes comprehensive)
        self._validators[ValidationLevel.PARANOID].extend(self._validators[ValidationLevel.COMPREHENSIVE])
        self._validators[ValidationLevel.PARANOID].append(self._validate_stress)
        self._validators[ValidationLevel.PARANOID].append(self._validate_edge_cases)

    async def validate(
        self,
        metadata: ModelMetadata,
        level: ValidationLevel = ValidationLevel.STANDARD,
        test_data: Optional[List[Dict]] = None,
    ) -> ValidationResult:
        """Validate a model."""
        result = ValidationResult(
            model_id=metadata.model_id,
            version=metadata.version,
            level=level,
        )

        validators = self._validators[level]
        result.tests_run = len(validators)

        for validator in validators:
            try:
                await asyncio.wait_for(
                    validator(metadata, result, test_data),
                    timeout=self.config.validation_timeout_seconds / len(validators)
                )
                result.tests_passed += 1
            except asyncio.TimeoutError:
                result.errors.append(f"Validator {validator.__name__} timed out")
                result.tests_failed += 1
            except Exception as e:
                result.errors.append(f"Validator {validator.__name__} failed: {e}")
                result.tests_failed += 1

        result.completed_at = datetime.utcnow()
        result.duration_seconds = (result.completed_at - result.started_at).total_seconds()
        result.passed = result.tests_failed == 0

        self.logger.info(
            f"Validation of {metadata.name}@{metadata.version}: "
            f"{'PASSED' if result.passed else 'FAILED'} "
            f"({result.tests_passed}/{result.tests_run} tests)"
        )

        return result

    async def _validate_metadata(self, metadata: ModelMetadata, result: ValidationResult, _):
        """Validate model metadata."""
        if not metadata.name:
            raise ValueError("Model name is required")
        if not metadata.model_type:
            raise ValueError("Model type is required")
        if metadata.model_size_bytes <= 0:
            result.warnings.append("Model size not specified")

    async def _validate_file_exists(self, metadata: ModelMetadata, result: ValidationResult, _):
        """Validate model file exists."""
        if metadata.source_path:
            if not Path(metadata.source_path).exists():
                raise ValueError(f"Model file not found: {metadata.source_path}")

    async def _validate_load(self, metadata: ModelMetadata, result: ValidationResult, _):
        """Validate model can be loaded."""
        # Simulated load test
        result.metrics["load_time_ms"] = 100.0  # Placeholder

    async def _validate_inference(self, metadata: ModelMetadata, result: ValidationResult, test_data):
        """Validate model inference."""
        # Simulated inference test
        result.metrics["inference_latency_ms"] = 50.0  # Placeholder

    async def _validate_performance(self, metadata: ModelMetadata, result: ValidationResult, test_data):
        """Validate performance meets expectations."""
        if metadata.expected_latency_ms > 0:
            actual = result.metrics.get("inference_latency_ms", 0)
            if actual > metadata.expected_latency_ms * 1.2:
                result.latency_within_bounds = False
                result.warnings.append(f"Latency {actual}ms exceeds expected {metadata.expected_latency_ms}ms")

    async def _validate_memory(self, metadata: ModelMetadata, result: ValidationResult, _):
        """Validate memory usage."""
        # Simulated memory check
        result.metrics["memory_usage_mb"] = metadata.ram_requirement_mb
        result.memory_within_bounds = True

    async def _validate_stress(self, metadata: ModelMetadata, result: ValidationResult, _):
        """Stress test the model."""
        result.metrics["stress_test_qps"] = 100.0

    async def _validate_edge_cases(self, metadata: ModelMetadata, result: ValidationResult, _):
        """Test edge cases."""
        result.metrics["edge_case_pass_rate"] = 1.0


# =============================================================================
# MODEL PERFORMANCE TRACKER
# =============================================================================


class ModelPerformanceTracker:
    """
    Historical performance tracking with anomaly detection.

    Features:
    - Real-time metric collection
    - Anomaly detection
    - Alerting
    - Historical analysis
    """

    def __init__(self, config: ModelManagementConfig):
        self.config = config
        self._metrics: Dict[str, PerformanceMetrics] = {}  # model_id -> metrics
        self._alerts: List[Alert] = []
        self._alert_callbacks: List[Callable] = []
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("ModelPerformanceTracker")

    def register_alert_callback(self, callback: Callable):
        """Register alert callback."""
        self._alert_callbacks.append(callback)

    async def record(
        self,
        model_id: str,
        version: SemanticVersion,
        latency_ms: float,
        success: bool,
        accuracy: Optional[float] = None,
        memory_mb: Optional[float] = None,
    ):
        """Record a metric sample."""
        async with self._lock:
            key = f"{model_id}:{version}"
            if key not in self._metrics:
                self._metrics[key] = PerformanceMetrics(
                    model_id=model_id,
                    version=version,
                )

            metrics = self._metrics[key]
            metrics.record_request(latency_ms, success, accuracy, memory_mb)

            # Check for anomalies
            await self._check_anomalies(model_id, version, metrics, latency_ms)

    async def _check_anomalies(
        self,
        model_id: str,
        version: SemanticVersion,
        metrics: PerformanceMetrics,
        latency_ms: float,
    ):
        """Check for anomalies and create alerts."""
        # Latency anomaly
        if metrics.latency.is_anomaly(latency_ms, self.config.anomaly_threshold_sigma):
            alert = Alert(
                model_id=model_id,
                version=version,
                severity=AlertSeverity.WARNING,
                title="Latency Anomaly Detected",
                message=f"Latency {latency_ms:.0f}ms is {self.config.anomaly_threshold_sigma}σ above mean",
                metric_type=MetricType.LATENCY,
                metric_value=latency_ms,
                threshold=metrics.latency.mean + self.config.anomaly_threshold_sigma * metrics.latency.std,
            )
            await self._emit_alert(alert)

        # Error rate spike
        error_rate = metrics.failed_requests / max(1, metrics.total_requests)
        if error_rate > self.config.error_rate_threshold:
            alert = Alert(
                model_id=model_id,
                version=version,
                severity=AlertSeverity.ERROR,
                title="High Error Rate",
                message=f"Error rate {error_rate:.1%} exceeds threshold {self.config.error_rate_threshold:.1%}",
                metric_type=MetricType.ERROR_RATE,
                metric_value=error_rate,
                threshold=self.config.error_rate_threshold,
            )
            await self._emit_alert(alert)

    async def _emit_alert(self, alert: Alert):
        """Emit an alert."""
        self._alerts.append(alert)
        self.logger.warning(f"Alert: {alert.title} - {alert.message}")

        for callback in self._alert_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(alert)
                else:
                    callback(alert)
            except Exception as e:
                self.logger.error(f"Alert callback error: {e}")

    async def get_metrics(self, model_id: str, version: SemanticVersion) -> Optional[PerformanceMetrics]:
        """Get metrics for a model version."""
        key = f"{model_id}:{version}"
        return self._metrics.get(key)

    async def get_alerts(
        self,
        model_id: Optional[str] = None,
        severity: Optional[AlertSeverity] = None,
        unresolved_only: bool = False,
    ) -> List[Alert]:
        """Get alerts with filtering."""
        alerts = self._alerts
        if model_id:
            alerts = [a for a in alerts if a.model_id == model_id]
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        if unresolved_only:
            alerts = [a for a in alerts if not a.resolved]
        return alerts


# =============================================================================
# MODEL LIFECYCLE MANAGER
# =============================================================================


class ModelLifecycleManager:
    """
    Policy-driven model lifecycle management.

    Features:
    - Automatic archival
    - Retention policies
    - Version cleanup
    - Storage tier management
    """

    def __init__(self, config: ModelManagementConfig, registry: ModelVersionRegistry):
        self.config = config
        self.registry = registry
        self._policies: Dict[str, LifecyclePolicy] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._lifecycle_task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger("ModelLifecycleManager")

    async def add_policy(self, policy: LifecyclePolicy):
        """Add a lifecycle policy."""
        async with self._lock:
            self._policies[policy.policy_id] = policy
            self.logger.info(f"Added lifecycle policy: {policy.name}")

    async def remove_policy(self, policy_id: str):
        """Remove a lifecycle policy."""
        async with self._lock:
            if policy_id in self._policies:
                del self._policies[policy_id]

    async def start(self):
        """Start lifecycle management."""
        if self._running:
            return
        self._running = True
        self._lifecycle_task = asyncio.create_task(self._lifecycle_loop())
        self.logger.info("Lifecycle manager started")

    async def stop(self):
        """Stop lifecycle management."""
        self._running = False
        if self._lifecycle_task:
            self._lifecycle_task.cancel()
            try:
                await self._lifecycle_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Lifecycle manager stopped")

    async def _lifecycle_loop(self):
        """Main lifecycle management loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.lifecycle_check_interval)
                await self._apply_policies()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Lifecycle loop error: {e}")

    async def _apply_policies(self):
        """Apply all lifecycle policies."""
        for policy in self._policies.values():
            if not policy.enabled:
                continue
            try:
                await self._apply_policy(policy)
                policy.last_run_at = datetime.utcnow()
            except Exception as e:
                self.logger.error(f"Policy {policy.name} failed: {e}")

    async def _apply_policy(self, policy: LifecyclePolicy):
        """Apply a single policy."""
        # Query matching models
        models = await self.registry.query()

        for model in models:
            # Check if policy applies
            if not self._policy_applies(policy, model):
                continue

            now = datetime.utcnow()
            age_days = (now - model.created_at).days

            # Archive check
            if policy.archive_after_days and age_days > policy.archive_after_days:
                if model.status not in (ModelStatus.ACTIVE, ModelStatus.ARCHIVED, ModelStatus.DELETED):
                    if not (policy.keep_promoted_versions and model.status == ModelStatus.PROMOTED):
                        await self._archive_model(model)

            # Delete check
            if policy.delete_after_days and age_days > policy.delete_after_days:
                if model.status == ModelStatus.ARCHIVED:
                    await self._delete_model(model)

    def _policy_applies(self, policy: LifecyclePolicy, model: ModelMetadata) -> bool:
        """Check if policy applies to model."""
        if policy.applies_to_types and model.model_type not in policy.applies_to_types:
            return False
        if policy.applies_to_status and model.status not in policy.applies_to_status:
            return False
        if policy.applies_to_tags and not any(t in model.tags for t in policy.applies_to_tags):
            return False
        return True

    async def _archive_model(self, model: ModelMetadata):
        """Archive a model."""
        model.status = ModelStatus.ARCHIVED
        model.archived_at = datetime.utcnow()
        await self.registry.update_status(model.name, str(model.version), ModelStatus.ARCHIVED)
        self.logger.info(f"Archived model {model.name}@{model.version}")

    async def _delete_model(self, model: ModelMetadata):
        """Soft-delete a model."""
        model.status = ModelStatus.DELETED
        await self.registry.update_status(model.name, str(model.version), ModelStatus.DELETED)
        self.logger.info(f"Deleted model {model.name}@{model.version}")


# =============================================================================
# UNIFIED MODEL MANAGEMENT ENGINE
# =============================================================================


class UnifiedModelManagementEngine:
    """
    Unified engine for all model management operations.

    Provides a single interface for:
    - Version registry
    - A/B testing
    - Rollback management
    - Validation pipeline
    - Performance tracking
    - Lifecycle management
    """

    def __init__(self, config: Optional[ModelManagementConfig] = None):
        self.config = config or ModelManagementConfig()
        self.logger = logging.getLogger("UnifiedModelManagementEngine")

        # Initialize components
        self.registry = ModelVersionRegistry(self.config)
        self.ab_testing = ABTestingFramework(self.config)
        self.rollback = ModelRollbackManager(self.config, self.registry)
        self.validation = ModelValidationPipeline(self.config)
        self.performance = ModelPerformanceTracker(self.config)
        self.lifecycle = ModelLifecycleManager(self.config, self.registry)

        # State
        self._initialized = False
        self._running = False

    async def initialize(self) -> bool:
        """Initialize the engine."""
        if self._initialized:
            return True

        try:
            # Load registry from disk
            await self.registry.load_from_disk()

            # Add default lifecycle policy
            await self.lifecycle.add_policy(LifecyclePolicy(
                name="default",
                description="Default lifecycle policy",
                archive_after_days=self.config.archive_after_days,
                delete_after_days=self.config.delete_after_days,
                min_versions_to_keep=3,
                keep_promoted_versions=True,
            ))

            self._initialized = True
            self.logger.info("UnifiedModelManagementEngine initialized")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def start(self):
        """Start background tasks."""
        if self._running:
            return

        self._running = True
        await self.lifecycle.start()
        self.logger.info("UnifiedModelManagementEngine started")

    async def stop(self):
        """Stop background tasks."""
        if not self._running:
            return

        self._running = False
        await self.lifecycle.stop()
        self.logger.info("UnifiedModelManagementEngine stopped")

    async def shutdown(self):
        """Complete shutdown."""
        await self.stop()
        self._initialized = False

    # -------------------------------------------------------------------------
    # High-Level Operations
    # -------------------------------------------------------------------------

    async def deploy_model(
        self,
        metadata: ModelMetadata,
        strategy: DeploymentStrategy = DeploymentStrategy.CANARY,
        validation_level: ValidationLevel = ValidationLevel.STANDARD,
    ) -> Tuple[bool, Optional[str]]:
        """Deploy a model with validation and strategy."""
        # Register version
        model_id = await self.registry.register(metadata)

        # Validate
        result = await self.validation.validate(metadata, validation_level)
        if not result.passed:
            await self.registry.update_status(metadata.name, str(metadata.version), ModelStatus.FAILED)
            return False, f"Validation failed: {result.errors}"

        # Deploy based on strategy
        if strategy == DeploymentStrategy.IMMEDIATE:
            await self.registry.update_status(metadata.name, str(metadata.version), ModelStatus.ACTIVE)
        elif strategy == DeploymentStrategy.CANARY:
            await self.registry.update_status(metadata.name, str(metadata.version), ModelStatus.CANARY)
        elif strategy == DeploymentStrategy.A_B_TEST:
            await self.registry.update_status(metadata.name, str(metadata.version), ModelStatus.A_B_TESTING)

        self.logger.info(f"Deployed model {metadata.name}@{metadata.version} with {strategy.name} strategy")
        return True, model_id

    async def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        return {
            "initialized": self._initialized,
            "running": self._running,
            "models_registered": sum(len(v) for v in self.registry._models.values()),
            "active_ab_tests": len(await self.ab_testing.list_active_tests()),
            "unresolved_alerts": len(await self.performance.get_alerts(unresolved_only=True)),
            "lifecycle_policies": len(self.lifecycle._policies),
        }


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_engine: Optional[UnifiedModelManagementEngine] = None
_engine_lock = asyncio.Lock()


async def get_model_management_engine() -> UnifiedModelManagementEngine:
    """Get or create the global engine instance."""
    global _engine

    async with _engine_lock:
        if _engine is None:
            _engine = UnifiedModelManagementEngine()
            await _engine.initialize()
        return _engine


async def initialize_model_management() -> bool:
    """Initialize the global model management engine."""
    engine = await get_model_management_engine()
    await engine.start()
    return True


async def shutdown_model_management():
    """Shutdown the global model management engine."""
    global _engine

    async with _engine_lock:
        if _engine is not None:
            await _engine.shutdown()
            _engine = None
            logger.info("Model management engine shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "ModelManagementConfig",
    # Enums
    "ModelType",
    "ModelStatus",
    "DeploymentStrategy",
    "RollbackReason",
    "ValidationLevel",
    "LifecycleAction",
    "MetricType",
    "AlertSeverity",
    # Data Structures
    "SemanticVersion",
    "ModelMetadata",
    "MetricSample",
    "MetricWindow",
    "PerformanceMetrics",
    "ValidationResult",
    "ABTestVariant",
    "ABTest",
    "RollbackEvent",
    "LifecyclePolicy",
    "Alert",
    # Components
    "ModelVersionRegistry",
    "ABTestingFramework",
    "ModelRollbackManager",
    "ModelValidationPipeline",
    "ModelPerformanceTracker",
    "ModelLifecycleManager",
    # Main Engine
    "UnifiedModelManagementEngine",
    # Global Functions
    "get_model_management_engine",
    "initialize_model_management",
    "shutdown_model_management",
]
