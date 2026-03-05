"""
Adaptive Timeout Manager — System-Wide Intelligent Timeout Infrastructure
==========================================================================

Provides dynamic, learned timeout values based on historical operation
performance, current system load, and per-operation quality gates.

Precedence Contract (Immutable, Universal):
    1. ENV VAR override (if set and parseable) — USER ALWAYS WINS
    2. Learned adaptive value (if enabled, not shadow, sufficient samples)
    3. Static default (the previously hardcoded value)

Feature Controls:
    ADAPTIVE_TIMEOUTS_ENABLED (bool, default True)
        False → skip adaptive computation entirely.

    ADAPTIVE_TIMEOUTS_SHADOW_ONLY (bool, default False)
        True → adaptive values computed and logged but NOT used.

    ADAPTIVE_TIMEOUTS_LOG_DECISIONS (bool, default True)
        Controls decision telemetry logging (rate-limited).

Kill Switch (live-toggleable without restart):
    Touch ~/.jarvis/adaptive_timeouts_disabled to disable.
    Remove file to re-enable. Checked every 5s (cached).

Per-operation debug:
    ADAPTIVE_TIMEOUTS_DEBUG_BACKEND_HEALTH=true
    (operation.value.upper() is the suffix)

Units Contract:
    - adaptive_get() / adaptive_get_sync() return SECONDS
    - get_timeout() returns MILLISECONDS (existing API, unchanged)
    - TimeoutConfig fields are in MILLISECONDS
    - Variables suffixed _ms = milliseconds, _s = seconds

Author: JARVIS Adaptive Timeout Infrastructure
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Deque,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger("jarvis.adaptive_timeout")


# =============================================================================
# Enums
# =============================================================================


class OperationType(Enum):
    """Types of operations for timeout tracking."""

    # Network operations
    API_CALL = "api_call"
    WEBSOCKET = "websocket"
    HTTP_REQUEST = "http_request"
    GRPC_CALL = "grpc_call"

    # Database operations
    DB_QUERY = "db_query"
    DB_WRITE = "db_write"
    DB_TRANSACTION = "db_transaction"

    # File operations
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_SEARCH = "file_search"

    # ML operations
    MODEL_INFERENCE = "model_inference"
    MODEL_LOAD = "model_load"
    EMBEDDING_COMPUTE = "embedding_compute"

    # Process operations
    PROCESS_START = "process_start"
    PROCESS_STOP = "process_stop"
    HEALTH_CHECK = "health_check"

    # Trinity operations
    TRINITY_SYNC = "trinity_sync"
    CROSS_REPO_COMMIT = "cross_repo_commit"
    CODING_COUNCIL = "coding_council"

    # Generic
    GENERIC = "generic"

    # === NEW: Startup / Infrastructure operations ===
    SERVICE_VERIFICATION = "service_verification"
    BACKEND_HEALTH = "backend_health"
    WEBSOCKET_CHECK = "websocket_check"
    PRIME_HEALTH = "prime_health"
    REACTOR_HEALTH = "reactor_health"
    STARTUP_FINALIZATION = "startup_finalization"
    VOICE_TRANSCRIPTION = "voice_transcription"
    VOICE_SPEAKER_ID = "voice_speaker_id"
    VOICE_BIOMETRIC = "voice_biometric"
    CLOUD_SQL_PROXY = "cloud_sql_proxy"
    GCP_VM_STARTUP = "gcp_vm_startup"
    SHUTDOWN_CLEANUP = "shutdown_cleanup"


class TimeoutStrategy(Enum):
    """Strategy for calculating timeout."""

    PERCENTILE_95 = "p95"
    PERCENTILE_99 = "p99"
    ADAPTIVE = "adaptive"
    FIXED = "fixed"
    AGGRESSIVE = "aggressive"


class LoadLevel(Enum):
    """System load level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionReason(Enum):
    """Fixed-cardinality reason tags for telemetry (0.15)."""

    ENV_OVERRIDE = "env_override"
    LEARNED_P95 = "learned_p95"
    COLD_START_DEFAULT = "cold_start_default"
    INSUFFICIENT_SAMPLES = "insufficient_samples"
    DISABLED = "disabled"
    SHADOW_MODE = "shadow_mode"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BOOTSTRAP_FALLBACK = "bootstrap_fallback"
    BOUNDS_CLAMPED = "bounds_clamped"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class OperationSample:
    """A single operation timing sample."""

    duration_ms: float
    timestamp: float
    success: bool
    complexity: float = 1.0
    context: Dict[str, Any] = field(default_factory=dict)
    supervisor_epoch: str = ""
    is_outlier: bool = False


@dataclass
class OperationStats:
    """Statistics for an operation type with per-operation cold-start tracking."""

    operation_type: OperationType
    samples: Deque[OperationSample] = field(
        default_factory=lambda: deque(maxlen=1000)
    )
    total_count: int = 0
    success_count: int = 0
    timeout_count: int = 0
    last_updated: float = field(default_factory=time.time)
    cold_start: bool = True
    epoch_operation_count: int = 0

    @property
    def success_rate(self) -> float:
        if self.total_count == 0:
            return 1.0
        return self.success_count / self.total_count

    @property
    def timeout_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.timeout_count / self.total_count

    def add_sample(self, sample: OperationSample) -> None:
        self.samples.append(sample)
        self.total_count += 1
        if sample.success:
            self.success_count += 1
        self.epoch_operation_count += 1
        self.last_updated = time.time()

    def get_percentile(self, percentile: float) -> float:
        """Get Nth percentile duration from SUCCESSFUL, non-outlier samples only."""
        if not self.samples:
            return 0.0
        durations = sorted(
            s.duration_ms
            for s in self.samples
            if s.success and not s.is_outlier
        )
        if not durations:
            return 0.0
        index = int(len(durations) * percentile / 100)
        return durations[min(index, len(durations) - 1)]

    def get_mean(self) -> float:
        durations = [
            s.duration_ms
            for s in self.samples
            if s.success and not s.is_outlier
        ]
        return statistics.mean(durations) if durations else 0.0

    def get_stddev(self) -> float:
        durations = [
            s.duration_ms
            for s in self.samples
            if s.success and not s.is_outlier
        ]
        return statistics.stdev(durations) if len(durations) > 1 else 0.0

    def successful_sample_count(self) -> int:
        return sum(
            1 for s in self.samples if s.success and not s.is_outlier
        )


@dataclass(frozen=True)
class FrozenOperationStats:
    """Immutable snapshot of operation stats for lock-free reads (0.4)."""

    total_count: int
    success_count: int
    timeout_count: int
    p95_ms: float
    p99_ms: float
    mean_ms: float
    cold_start: bool
    epoch_operation_count: int


@dataclass
class TimeoutBudget:
    """Tracks remaining timeout budget for cascading operations (0.13)."""

    total_budget_ms: float
    spent_ms: float = 0.0
    started_at: float = field(default_factory=time.time)
    operations: List[Tuple[str, float]] = field(default_factory=list)

    @property
    def remaining_ms(self) -> float:
        elapsed = (time.time() - self.started_at) * 1000
        return max(0, self.total_budget_ms - elapsed)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_ms <= 0

    def allocate(self, operation: str, amount_ms: float) -> float:
        """Allocate from budget, returning actual allocation (may be 0)."""
        actual = min(amount_ms, self.remaining_ms)
        self.operations.append((operation, actual))
        self.spent_ms += actual
        return actual


@dataclass
class TimeoutConfig:
    """Configuration for a specific operation type."""

    operation_type: OperationType
    default_ms: float
    min_ms: float
    max_ms: float
    strategy: TimeoutStrategy = TimeoutStrategy.ADAPTIVE
    complexity_weight: float = 1.0
    load_sensitivity: float = 1.0
    priority: int = 5
    warmup_threshold: int = 10
    cold_start_multiplier: float = 2.0
    outlier_multiplier: float = 3.0
    env_var: Optional[str] = None  # Associated JARVIS_* env var name


# =============================================================================
# Default Configurations
# =============================================================================

DEFAULT_CONFIGS: Dict[OperationType, TimeoutConfig] = {
    # --- Original coding-council operations ---
    OperationType.API_CALL: TimeoutConfig(
        OperationType.API_CALL,
        default_ms=5000, min_ms=500, max_ms=30000,
        complexity_weight=1.2, priority=7,
    ),
    OperationType.WEBSOCKET: TimeoutConfig(
        OperationType.WEBSOCKET,
        default_ms=10000, min_ms=1000, max_ms=60000,
        complexity_weight=0.8, priority=6,
    ),
    OperationType.DB_QUERY: TimeoutConfig(
        OperationType.DB_QUERY,
        default_ms=2000, min_ms=100, max_ms=15000,
        complexity_weight=1.5, priority=8,
    ),
    OperationType.DB_WRITE: TimeoutConfig(
        OperationType.DB_WRITE,
        default_ms=3000, min_ms=200, max_ms=20000,
        complexity_weight=1.3, priority=8,
    ),
    OperationType.FILE_READ: TimeoutConfig(
        OperationType.FILE_READ,
        default_ms=1000, min_ms=50, max_ms=10000,
        complexity_weight=1.0, priority=5,
    ),
    OperationType.FILE_WRITE: TimeoutConfig(
        OperationType.FILE_WRITE,
        default_ms=2000, min_ms=100, max_ms=15000,
        complexity_weight=1.1, priority=6,
    ),
    OperationType.MODEL_INFERENCE: TimeoutConfig(
        OperationType.MODEL_INFERENCE,
        default_ms=10000, min_ms=1000, max_ms=120000,
        complexity_weight=2.0, priority=7,
    ),
    OperationType.MODEL_LOAD: TimeoutConfig(
        OperationType.MODEL_LOAD,
        default_ms=30000, min_ms=5000, max_ms=300000,
        complexity_weight=1.5, priority=9,
        outlier_multiplier=4.0,
    ),
    OperationType.PROCESS_START: TimeoutConfig(
        OperationType.PROCESS_START,
        default_ms=15000, min_ms=2000, max_ms=60000,
        complexity_weight=1.2, priority=9,
    ),
    OperationType.HEALTH_CHECK: TimeoutConfig(
        OperationType.HEALTH_CHECK,
        default_ms=2000, min_ms=200, max_ms=10000,
        strategy=TimeoutStrategy.AGGRESSIVE, priority=8,
    ),
    OperationType.TRINITY_SYNC: TimeoutConfig(
        OperationType.TRINITY_SYNC,
        default_ms=5000, min_ms=500, max_ms=30000,
        complexity_weight=1.3, priority=7,
    ),
    OperationType.CROSS_REPO_COMMIT: TimeoutConfig(
        OperationType.CROSS_REPO_COMMIT,
        default_ms=30000, min_ms=5000, max_ms=120000,
        complexity_weight=1.5, priority=9,
    ),
    OperationType.CODING_COUNCIL: TimeoutConfig(
        OperationType.CODING_COUNCIL,
        default_ms=60000, min_ms=10000, max_ms=300000,
        complexity_weight=2.5, priority=6,
    ),
    OperationType.GENERIC: TimeoutConfig(
        OperationType.GENERIC,
        default_ms=5000, min_ms=500, max_ms=60000, priority=5,
    ),
    # --- NEW: Startup / Infrastructure ---
    OperationType.SERVICE_VERIFICATION: TimeoutConfig(
        OperationType.SERVICE_VERIFICATION,
        default_ms=15000, min_ms=3000, max_ms=60000,
        strategy=TimeoutStrategy.PERCENTILE_95, priority=9,
        env_var="JARVIS_VERIFICATION_TIMEOUT",
    ),
    OperationType.BACKEND_HEALTH: TimeoutConfig(
        OperationType.BACKEND_HEALTH,
        default_ms=3000, min_ms=500, max_ms=15000,
        strategy=TimeoutStrategy.ADAPTIVE, priority=8,
        env_var="JARVIS_VERIFY_BACKEND_TIMEOUT",
    ),
    OperationType.WEBSOCKET_CHECK: TimeoutConfig(
        OperationType.WEBSOCKET_CHECK,
        default_ms=5000, min_ms=500, max_ms=15000,
        strategy=TimeoutStrategy.ADAPTIVE, priority=7,
        env_var="JARVIS_VERIFY_WEBSOCKET_TIMEOUT",
    ),
    OperationType.PRIME_HEALTH: TimeoutConfig(
        OperationType.PRIME_HEALTH,
        default_ms=2000, min_ms=500, max_ms=10000,
        strategy=TimeoutStrategy.ADAPTIVE, priority=7,
        env_var="JARVIS_VERIFY_PRIME_TIMEOUT",
    ),
    OperationType.REACTOR_HEALTH: TimeoutConfig(
        OperationType.REACTOR_HEALTH,
        default_ms=2000, min_ms=500, max_ms=10000,
        strategy=TimeoutStrategy.ADAPTIVE, priority=7,
        env_var="JARVIS_VERIFY_REACTOR_TIMEOUT",
    ),
    OperationType.STARTUP_FINALIZATION: TimeoutConfig(
        OperationType.STARTUP_FINALIZATION,
        default_ms=30000, min_ms=5000, max_ms=120000,
        strategy=TimeoutStrategy.PERCENTILE_95, priority=9,
    ),
    OperationType.VOICE_TRANSCRIPTION: TimeoutConfig(
        OperationType.VOICE_TRANSCRIPTION,
        default_ms=5000, min_ms=500, max_ms=30000,
        complexity_weight=1.5, priority=6,
    ),
    OperationType.VOICE_SPEAKER_ID: TimeoutConfig(
        OperationType.VOICE_SPEAKER_ID,
        default_ms=3000, min_ms=500, max_ms=15000,
        priority=6,
    ),
    OperationType.VOICE_BIOMETRIC: TimeoutConfig(
        OperationType.VOICE_BIOMETRIC,
        default_ms=5000, min_ms=1000, max_ms=20000,
        priority=7,
    ),
    OperationType.CLOUD_SQL_PROXY: TimeoutConfig(
        OperationType.CLOUD_SQL_PROXY,
        default_ms=10000, min_ms=2000, max_ms=60000,
        priority=8,
        outlier_multiplier=4.0,
    ),
    OperationType.GCP_VM_STARTUP: TimeoutConfig(
        OperationType.GCP_VM_STARTUP,
        default_ms=120000, min_ms=30000, max_ms=600000,
        priority=9,
        outlier_multiplier=5.0,
    ),
    OperationType.SHUTDOWN_CLEANUP: TimeoutConfig(
        OperationType.SHUTDOWN_CLEANUP,
        default_ms=10000, min_ms=2000, max_ms=30000,
        strategy=TimeoutStrategy.AGGRESSIVE, priority=8,
    ),
    # Remaining network/file/process ops use GENERIC defaults
    OperationType.HTTP_REQUEST: TimeoutConfig(
        OperationType.HTTP_REQUEST,
        default_ms=5000, min_ms=500, max_ms=30000,
        complexity_weight=1.2, priority=6,
    ),
    OperationType.GRPC_CALL: TimeoutConfig(
        OperationType.GRPC_CALL,
        default_ms=5000, min_ms=500, max_ms=30000,
        complexity_weight=1.2, priority=6,
    ),
    OperationType.DB_TRANSACTION: TimeoutConfig(
        OperationType.DB_TRANSACTION,
        default_ms=5000, min_ms=500, max_ms=30000,
        complexity_weight=1.5, priority=8,
    ),
    OperationType.FILE_SEARCH: TimeoutConfig(
        OperationType.FILE_SEARCH,
        default_ms=5000, min_ms=200, max_ms=30000,
        complexity_weight=1.3, priority=5,
    ),
    OperationType.EMBEDDING_COMPUTE: TimeoutConfig(
        OperationType.EMBEDDING_COMPUTE,
        default_ms=5000, min_ms=500, max_ms=60000,
        complexity_weight=2.0, priority=6,
    ),
    OperationType.PROCESS_STOP: TimeoutConfig(
        OperationType.PROCESS_STOP,
        default_ms=10000, min_ms=1000, max_ms=30000,
        priority=7,
    ),
}


# =============================================================================
# Context Data Hygiene (0.12)
# =============================================================================

_CONTEXT_WHITELIST: FrozenSet[str] = frozenset({
    "endpoint", "payload_size", "batch_size", "input_tokens",
    "num_files", "lines_of_code", "query_type", "expected_rows",
    "file_size", "service_name", "phase_name",
})


def _sanitize_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Strip non-whitelisted fields before persistence."""
    return {k: v for k, v in context.items() if k in _CONTEXT_WHITELIST}


# =============================================================================
# Complexity Estimators
# =============================================================================


class ComplexityEstimator:
    """Estimates operation complexity from context."""

    @staticmethod
    def estimate(operation: OperationType, context: Dict[str, Any]) -> float:
        if operation == OperationType.API_CALL:
            return ComplexityEstimator._api_complexity(context)
        elif operation == OperationType.DB_QUERY:
            return ComplexityEstimator._db_complexity(context)
        elif operation in (OperationType.FILE_READ, OperationType.FILE_WRITE):
            return ComplexityEstimator._file_complexity(context)
        elif operation == OperationType.MODEL_INFERENCE:
            return ComplexityEstimator._ml_complexity(context)
        elif operation == OperationType.CODING_COUNCIL:
            return ComplexityEstimator._council_complexity(context)
        return 1.0

    @staticmethod
    def _api_complexity(ctx: Dict[str, Any]) -> float:
        base = 1.0
        payload_size = ctx.get("payload_size", 0)
        if payload_size > 100000:
            base *= 1.5
        elif payload_size > 10000:
            base *= 1.2
        endpoint = ctx.get("endpoint", "")
        if "/analyze" in endpoint or "/generate" in endpoint:
            base *= 1.5
        if "/bulk" in endpoint or "/batch" in endpoint:
            base *= 2.0
        return base

    @staticmethod
    def _db_complexity(ctx: Dict[str, Any]) -> float:
        base = 1.0
        if ctx.get("query_type", "") in ("join", "aggregate", "subquery"):
            base *= 1.5
        expected_rows = ctx.get("expected_rows", 0)
        if expected_rows > 10000:
            base *= 2.0
        elif expected_rows > 1000:
            base *= 1.3
        return base

    @staticmethod
    def _file_complexity(ctx: Dict[str, Any]) -> float:
        base = 1.0
        file_size = ctx.get("file_size", 0)
        if file_size > 10_000_000:
            base *= 2.0
        elif file_size > 1_000_000:
            base *= 1.5
        return base

    @staticmethod
    def _ml_complexity(ctx: Dict[str, Any]) -> float:
        base = 1.0
        batch_size = ctx.get("batch_size", 1)
        base *= math.log2(batch_size + 1)
        input_tokens = ctx.get("input_tokens", 0)
        if input_tokens > 4096:
            base *= 2.0
        elif input_tokens > 1024:
            base *= 1.5
        return base

    @staticmethod
    def _council_complexity(ctx: Dict[str, Any]) -> float:
        base = 1.0
        num_files = ctx.get("num_files", 1)
        base *= math.log2(num_files + 1)
        if ctx.get("lines_of_code", 0) > 10000:
            base *= 2.0
        elif ctx.get("lines_of_code", 0) > 1000:
            base *= 1.5
        return base


# =============================================================================
# Feature Flags + Kill Switch (0.2)
# =============================================================================

_KILL_SWITCH_FILE = Path.home() / ".jarvis" / "adaptive_timeouts_disabled"
_enabled_cache: Optional[Tuple[bool, float]] = None
_CACHE_TTL = 5.0


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_float(key: str, default: float) -> Optional[float]:
    """Parse env var as float. Returns None if not set, default if unparseable."""
    raw = os.environ.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _is_kill_switch_active() -> bool:
    """Check kill switch file with security hardening (0.2)."""
    try:
        path = _KILL_SWITCH_FILE
        if path.is_symlink():
            logger.warning(
                "[AdaptiveTimeout] Kill switch file is symlink — ignoring"
            )
            return False
        if not path.exists():
            return False
        stat = path.stat()
        if stat.st_uid != os.getuid():
            logger.warning(
                "[AdaptiveTimeout] Kill switch file owned by uid=%d, expected=%d — ignoring",
                stat.st_uid, os.getuid(),
            )
            return False
        if stat.st_mode & 0o002:
            logger.warning(
                "[AdaptiveTimeout] Kill switch file is world-writable — ignoring"
            )
            return False
        return True
    except OSError:
        return False


def _is_enabled() -> bool:
    """Check if adaptive timeouts are enabled. Supports live toggle via file (0.2)."""
    global _enabled_cache
    now = time.time()
    if _enabled_cache is not None:
        cached_val, cached_at = _enabled_cache
        if now - cached_at < _CACHE_TTL:
            return cached_val

    if _is_kill_switch_active():
        _enabled_cache = (False, now)
        return False
    result = _env_bool("ADAPTIVE_TIMEOUTS_ENABLED", True)
    _enabled_cache = (result, now)
    return result


def _is_shadow_only() -> bool:
    return _env_bool("ADAPTIVE_TIMEOUTS_SHADOW_ONLY", False)


def _is_log_decisions() -> bool:
    return _env_bool("ADAPTIVE_TIMEOUTS_LOG_DECISIONS", True)


def _is_read_only() -> bool:
    return _env_bool("ADAPTIVE_TIMEOUTS_READ_ONLY", False)


def _is_debug_for_op(operation: OperationType) -> bool:
    """Per-operation debug toggle: ADAPTIVE_TIMEOUTS_DEBUG_{OPERATION_VALUE_UPPER}."""
    key = f"ADAPTIVE_TIMEOUTS_DEBUG_{operation.value.upper()}"
    return _env_bool(key, False)


# =============================================================================
# Adaptive Timeout Manager
# =============================================================================


class AdaptiveTimeoutManager:
    """
    Intelligent timeout management with adaptive learning, copy-on-write
    snapshot for thread safety, and SQLite persistence.

    Thread-safe and async-compatible. Per-process singleton via sys attribute.
    """

    def __init__(
        self,
        logger_instance: Optional[logging.Logger] = None,
        history_window: int = 1000,
    ):
        self.log = logger_instance or logger
        self.history_window = history_window
        self._supervisor_epoch = str(uuid.uuid4())[:8]

        # Mutable stats — protected by _stats_lock for writes
        self._stats: Dict[OperationType, OperationStats] = {}
        for op_type in OperationType:
            self._stats[op_type] = OperationStats(operation_type=op_type)

        # Immutable snapshot — swapped atomically via GIL reference assignment
        self._stats_snapshot: Dict[OperationType, FrozenOperationStats] = {}

        # Threading safety
        self._stats_lock = threading.Lock()
        self._snapshot_rebuild_interval = 10.0
        self._last_snapshot_rebuild = 0.0

        # Configs
        self._configs: Dict[OperationType, TimeoutConfig] = DEFAULT_CONFIGS.copy()

        # Budget tracking
        self._active_budgets: Dict[str, TimeoutBudget] = {}

        # System load
        self._load_level: LoadLevel = LoadLevel.MEDIUM
        self._load_last_checked: float = 0
        self._load_check_interval: float = 5.0

        # Telemetry counters (0.15)
        self._decision_counters = {
            "total": 0,
            "source_env": 0,
            "source_learned": 0,
            "source_default": 0,
            "shadow_would_differ": 0,
        }
        self._log_rate_limiter: Dict[str, float] = {}
        self._log_rate_limit_interval = 10.0

        # Persistence
        self._db_path = Path.home() / ".jarvis" / "learning" / "adaptive_timeouts.db"
        self._persist_interval = 120.0
        self._last_persist = 0.0
        self._migration_degraded = False
        self._schema_version = 0
        self._expected_schema_version = 1

        # Async lock for persistence operations
        self._persist_lock: Optional[asyncio.Lock] = None

        # Auto-persist task handle
        self._persist_task: Optional[asyncio.Task] = None

    # =====================================================================
    # Copy-on-Write Snapshot (Step 3)
    # =====================================================================

    def _rebuild_snapshot(self) -> None:
        """Rebuild immutable snapshot from mutable stats."""
        new_snapshot: Dict[OperationType, FrozenOperationStats] = {}
        with self._stats_lock:
            for op, stats in self._stats.items():
                new_snapshot[op] = FrozenOperationStats(
                    total_count=stats.total_count,
                    success_count=stats.success_count,
                    timeout_count=stats.timeout_count,
                    p95_ms=stats.get_percentile(95),
                    p99_ms=stats.get_percentile(99),
                    mean_ms=stats.get_mean(),
                    cold_start=stats.cold_start,
                    epoch_operation_count=stats.epoch_operation_count,
                )
        # GIL-atomic reference swap
        self._stats_snapshot = new_snapshot
        self._last_snapshot_rebuild = time.time()

    def _maybe_rebuild_snapshot(self) -> None:
        """Rebuild snapshot if interval has elapsed."""
        now = time.time()
        if now - self._last_snapshot_rebuild >= self._snapshot_rebuild_interval:
            self._rebuild_snapshot()

    # =====================================================================
    # Load Level (Step 4) — uses async_system_metrics
    # =====================================================================

    async def _update_load_level(self) -> None:
        now = time.time()
        if now - self._load_last_checked < self._load_check_interval:
            return
        self._load_last_checked = now
        try:
            from backend.core.async_system_metrics import (
                get_cpu_percent,
                get_memory_percent,
            )
            cpu = await get_cpu_percent()
            mem = await get_memory_percent()
            pressure = max(cpu, mem)
            if pressure > 90:
                self._load_level = LoadLevel.CRITICAL
            elif pressure > 70:
                self._load_level = LoadLevel.HIGH
            elif pressure > 30:
                self._load_level = LoadLevel.MEDIUM
            else:
                self._load_level = LoadLevel.LOW
        except Exception:
            pass

    def _get_load_factor_sync(self, config: TimeoutConfig) -> float:
        """Synchronous load factor using cached load level."""
        base_factors = {
            LoadLevel.LOW: 0.8,
            LoadLevel.MEDIUM: 1.0,
            LoadLevel.HIGH: 1.5,
            LoadLevel.CRITICAL: 2.5,
        }
        base = base_factors[self._load_level]
        if config.load_sensitivity != 1.0:
            adjustment = (base - 1.0) * config.load_sensitivity
            return 1.0 + adjustment
        return base

    async def _get_load_factor(self, config: TimeoutConfig) -> float:
        await self._update_load_level()
        return self._get_load_factor_sync(config)

    # =====================================================================
    # Quality Gates (Step 5)
    # =====================================================================

    def _apply_outlier_rejection(
        self, operation: OperationType, stats: OperationStats
    ) -> None:
        """Mark outliers in samples. Minimum 20-sample guard (0.5 Rule 3)."""
        config = self._configs.get(
            operation, DEFAULT_CONFIGS[OperationType.GENERIC]
        )
        sample_count = stats.successful_sample_count()

        if sample_count < 20:
            # Not enough data for reliable outlier detection
            return

        p99 = stats.get_percentile(99)
        if p99 <= 0:
            return

        threshold = p99 * config.outlier_multiplier
        for s in stats.samples:
            if s.success and s.duration_ms > threshold:
                s.is_outlier = True

        # Degenerate case: if ALL successful samples are now outliers, unmark all
        non_outlier_count = sum(
            1 for s in stats.samples if s.success and not s.is_outlier
        )
        if non_outlier_count == 0:
            for s in stats.samples:
                s.is_outlier = False

    def _adaptive_timeout_sync(
        self, frozen: FrozenOperationStats, config: TimeoutConfig
    ) -> Tuple[float, DecisionReason]:
        """Calculate adaptive timeout from frozen snapshot. Returns (ms, reason)."""
        # Cold start check
        if frozen.cold_start or frozen.epoch_operation_count < config.warmup_threshold:
            return config.default_ms * config.cold_start_multiplier, DecisionReason.COLD_START_DEFAULT

        # Insufficient samples
        successful_approx = frozen.success_count
        if successful_approx < config.warmup_threshold:
            return config.default_ms, DecisionReason.INSUFFICIENT_SAMPLES

        # Use P95 as base
        timeout = frozen.p95_ms
        if timeout <= 0:
            return config.default_ms, DecisionReason.INSUFFICIENT_SAMPLES

        # Adjust based on timeout rate (bounded: 0.5 Rule 2)
        timeout_rate = (
            frozen.timeout_count / frozen.total_count
            if frozen.total_count > 0 else 0.0
        )
        if timeout_rate > 0.25:
            timeout = config.max_ms  # CAP at max
        elif timeout_rate > 0.1:
            timeout *= 1.5  # Moderate increase (not 2.0)

        # Clamp to bounds
        timeout = max(config.min_ms, min(config.max_ms, timeout))

        return timeout, DecisionReason.LEARNED_P95

    # =====================================================================
    # Core get_timeout (existing API, returns ms)
    # =====================================================================

    async def get_timeout(
        self,
        operation: OperationType,
        context: Optional[Dict[str, Any]] = None,
        budget: Optional[TimeoutBudget] = None,
        strategy: Optional[TimeoutStrategy] = None,
    ) -> float:
        """Get adaptive timeout for an operation. Returns MILLISECONDS."""
        context = context or {}
        config = self._configs.get(
            operation, DEFAULT_CONFIGS[OperationType.GENERIC]
        )
        stats = self._stats[operation]
        strat = strategy or config.strategy

        if strat == TimeoutStrategy.FIXED:
            base_timeout = config.default_ms
        elif strat == TimeoutStrategy.AGGRESSIVE:
            base_timeout = stats.get_percentile(75) or config.default_ms * 0.5
        elif strat == TimeoutStrategy.PERCENTILE_99:
            base_timeout = stats.get_percentile(99) or config.default_ms
        elif strat == TimeoutStrategy.PERCENTILE_95:
            base_timeout = stats.get_percentile(95) or config.default_ms
        else:  # ADAPTIVE
            frozen = self._stats_snapshot.get(operation)
            if frozen:
                base_timeout, _ = self._adaptive_timeout_sync(frozen, config)
            else:
                base_timeout = config.default_ms

        complexity = ComplexityEstimator.estimate(operation, context)
        timeout = base_timeout * complexity * config.complexity_weight

        load_factor = await self._get_load_factor(config)
        timeout *= load_factor
        timeout = max(config.min_ms, min(config.max_ms, timeout))

        if budget:
            timeout = budget.allocate(operation.value, timeout)

        return timeout

    # =====================================================================
    # track_operation context manager
    # =====================================================================

    @asynccontextmanager
    async def track_operation(
        self,
        operation: OperationType,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Context manager to track operation duration for learning."""
        context = context or {}
        start_time = time.time()
        success = True
        complexity = ComplexityEstimator.estimate(operation, context)

        try:
            yield
        except asyncio.TimeoutError:
            success = False
            with self._stats_lock:
                self._stats[operation].timeout_count += 1
            raise
        except Exception:
            success = False
            raise
        finally:
            duration_ms = (time.time() - start_time) * 1000
            sample = OperationSample(
                duration_ms=duration_ms,
                timestamp=start_time,
                success=success,
                complexity=complexity,
                context=context,
                supervisor_epoch=self._supervisor_epoch,
            )

            was_cold = False
            with self._stats_lock:
                stats = self._stats[operation]
                was_cold = stats.cold_start
                stats.add_sample(sample)
                config = self._configs.get(
                    operation, DEFAULT_CONFIGS[OperationType.GENERIC]
                )
                if stats.epoch_operation_count >= config.warmup_threshold:
                    stats.cold_start = False
                self._apply_outlier_rejection(operation, stats)

            # Trigger immediate snapshot rebuild on cold→warm transition (0.16)
            if was_cold and not self._stats[operation].cold_start:
                self._rebuild_snapshot()
            else:
                self._maybe_rebuild_snapshot()

    def record_duration(
        self,
        operation: OperationType,
        duration_ms: float,
        success: bool = True,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Manually record an operation duration."""
        context = context or {}
        sample = OperationSample(
            duration_ms=duration_ms,
            timestamp=time.time(),
            success=success,
            complexity=ComplexityEstimator.estimate(operation, context),
            context=context,
            supervisor_epoch=self._supervisor_epoch,
        )
        with self._stats_lock:
            self._stats[operation].add_sample(sample)
            config = self._configs.get(
                operation, DEFAULT_CONFIGS[OperationType.GENERIC]
            )
            if self._stats[operation].epoch_operation_count >= config.warmup_threshold:
                self._stats[operation].cold_start = False
            self._apply_outlier_rejection(operation, self._stats[operation])
        self._maybe_rebuild_snapshot()

    def create_budget(
        self, total_ms: float, budget_id: Optional[str] = None
    ) -> TimeoutBudget:
        budget = TimeoutBudget(total_budget_ms=total_ms)
        budget_id = budget_id or f"budget_{time.time()}"
        self._active_budgets[budget_id] = budget
        return budget

    def get_config(self, operation: OperationType) -> TimeoutConfig:
        return self._configs.get(
            operation, DEFAULT_CONFIGS[OperationType.GENERIC]
        )

    def set_config(self, operation: OperationType, config: TimeoutConfig) -> None:
        self._configs[operation] = config

    def get_stats(self, operation: OperationType) -> OperationStats:
        return self._stats[operation]

    # =====================================================================
    # SQLite Persistence (Step 6)
    # =====================================================================

    async def _get_persist_lock(self) -> asyncio.Lock:
        if self._persist_lock is None:
            self._persist_lock = asyncio.Lock()
        return self._persist_lock

    async def persist(self) -> None:
        """Persist statistics to SQLite."""
        if _is_read_only():
            return

        lock = await self._get_persist_lock()
        async with lock:
            try:
                import aiosqlite
            except ImportError:
                self.log.debug("[AdaptiveTimeout] aiosqlite not available, skipping persist")
                return

            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)

                async with aiosqlite.connect(str(self._db_path)) as db:
                    await db.execute("PRAGMA journal_mode = WAL")
                    await db.execute("PRAGMA busy_timeout = 5000")
                    await db.execute("PRAGMA wal_autocheckpoint = 100")

                    await self._migrate(db)

                    # Persist aggregate stats
                    for op_type, stats in self._stats.items():
                        if stats.total_count > 0:
                            await db.execute(
                                """INSERT OR REPLACE INTO operation_stats
                                   (operation, total_count, success_count, timeout_count,
                                    mean_ms, p95_ms, p99_ms, supervisor_epoch, updated_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    op_type.value,
                                    stats.total_count,
                                    stats.success_count,
                                    stats.timeout_count,
                                    stats.get_mean(),
                                    stats.get_percentile(95),
                                    stats.get_percentile(99),
                                    self._supervisor_epoch,
                                    time.time(),
                                ),
                            )

                    # Persist recent samples with bounded retention
                    for op_type, stats in self._stats.items():
                        recent = list(stats.samples)[-50:]  # Persist latest 50
                        for s in recent:
                            sanitized = _sanitize_context(s.context)
                            try:
                                import json as _json
                                ctx_json = _json.dumps(sanitized)
                            except Exception:
                                ctx_json = "{}"
                            await db.execute(
                                """INSERT INTO operation_samples
                                   (operation, duration_ms, timestamp, success,
                                    complexity, context_json, supervisor_epoch, is_outlier)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    op_type.value,
                                    s.duration_ms,
                                    s.timestamp,
                                    int(s.success),
                                    s.complexity,
                                    ctx_json,
                                    s.supervisor_epoch,
                                    int(s.is_outlier),
                                ),
                            )

                    # Bounded retention: max 200 samples per operation
                    for op_type in OperationType:
                        count_rows = await db.execute_fetchall(
                            "SELECT COUNT(*) FROM operation_samples WHERE operation = ?",
                            (op_type.value,),
                        )
                        count = list(count_rows)[0][0] if count_rows else 0
                        if count > 200:
                            excess = count - 200
                            await db.execute(
                                """DELETE FROM operation_samples
                                   WHERE rowid IN (
                                       SELECT rowid FROM operation_samples
                                       WHERE operation = ?
                                       ORDER BY timestamp ASC
                                       LIMIT ?
                                   )""",
                                (op_type.value, excess),
                            )

                    await db.commit()

                self._last_persist = time.time()
                self.log.debug("[AdaptiveTimeout] Persisted to SQLite")

            except Exception as e:
                self.log.warning("[AdaptiveTimeout] Persist failed: %s", e)

    async def _migrate(self, db: Any) -> None:
        """Run schema migrations (0.7)."""
        try:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS schema_version (
                       version INTEGER PRIMARY KEY,
                       applied_at REAL NOT NULL
                   )"""
            )
            ver_rows = list(await db.execute_fetchall(
                "SELECT MAX(version) FROM schema_version"
            ))
            current = ver_rows[0][0] if ver_rows and ver_rows[0][0] is not None else 0
            self._schema_version = current

            if current < 1:
                # Migration v1: Create core tables
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS operation_stats (
                           operation TEXT PRIMARY KEY,
                           total_count INTEGER DEFAULT 0,
                           success_count INTEGER DEFAULT 0,
                           timeout_count INTEGER DEFAULT 0,
                           mean_ms REAL DEFAULT 0,
                           p95_ms REAL DEFAULT 0,
                           p99_ms REAL DEFAULT 0,
                           supervisor_epoch TEXT DEFAULT '',
                           updated_at REAL DEFAULT 0
                       )"""
                )
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS operation_samples (
                           rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                           operation TEXT NOT NULL,
                           duration_ms REAL NOT NULL,
                           timestamp REAL NOT NULL,
                           success INTEGER NOT NULL,
                           complexity REAL DEFAULT 1.0,
                           context_json TEXT DEFAULT '{}',
                           supervisor_epoch TEXT DEFAULT '',
                           is_outlier INTEGER DEFAULT 0
                       )"""
                )
                await db.execute(
                    """CREATE INDEX IF NOT EXISTS idx_samples_op_ts
                       ON operation_samples(operation, timestamp)"""
                )
                await db.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (1, ?)",
                    (time.time(),),
                )
                await db.commit()
                self._schema_version = 1

            self._migration_degraded = False

        except Exception as e:
            self.log.warning(
                "[AdaptiveTimeout] Migration from v%d to v%d failed: %s",
                self._schema_version, self._expected_schema_version, e,
            )
            self._migration_degraded = True

    async def load_stats(self) -> None:
        """Load statistics from SQLite (or fallback to JSON)."""
        try:
            import aiosqlite
        except ImportError:
            await self._load_stats_json_fallback()
            return

        try:
            if not self._db_path.exists():
                # Try JSON fallback for migration from old format
                await self._load_stats_json_fallback()
                self._rebuild_snapshot()
                return

            async with aiosqlite.connect(str(self._db_path)) as db:
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA busy_timeout = 5000")

                await self._migrate(db)

                if self._migration_degraded:
                    self.log.warning(
                        "[AdaptiveTimeout] Migration degraded — operating in-memory only"
                    )
                    self._rebuild_snapshot()
                    return

                # Load aggregate stats
                rows = await db.execute_fetchall(
                    "SELECT operation, total_count, success_count, timeout_count FROM operation_stats"
                )
                for row in rows:
                    try:
                        op_type = OperationType(row[0])
                        stats = self._stats[op_type]
                        stats.total_count = row[1]
                        stats.success_count = row[2]
                        stats.timeout_count = row[3]
                    except (ValueError, KeyError):
                        continue

                # Load recent samples (for P95 calculation)
                sample_rows = await db.execute_fetchall(
                    """SELECT operation, duration_ms, timestamp, success, complexity,
                              supervisor_epoch, is_outlier
                       FROM operation_samples
                       ORDER BY timestamp DESC
                       LIMIT 2000"""
                )
                for row in sample_rows:
                    try:
                        op_type = OperationType(row[0])
                        sample = OperationSample(
                            duration_ms=row[1],
                            timestamp=row[2],
                            success=bool(row[3]),
                            complexity=row[4],
                            supervisor_epoch=row[5] or "",
                            is_outlier=bool(row[6]),
                        )
                        # Prior-epoch samples get 0.5x weighting consideration
                        # (tracked via epoch field, used in _adaptive_timeout_sync)
                        self._stats[op_type].samples.append(sample)
                    except (ValueError, KeyError):
                        continue

                # Determine cold start per operation
                for op_type, stats in self._stats.items():
                    config = self._configs.get(
                        op_type, DEFAULT_CONFIGS[OperationType.GENERIC]
                    )
                    if stats.successful_sample_count() >= config.warmup_threshold:
                        stats.cold_start = False

        except Exception as e:
            self.log.warning("[AdaptiveTimeout] Failed to load stats from SQLite: %s", e)
            await self._load_stats_json_fallback()

        self._rebuild_snapshot()

    async def _load_stats_json_fallback(self) -> None:
        """Fallback: load from legacy JSON file."""
        import json as _json
        json_path = Path.home() / ".jarvis" / "trinity" / "timeout_stats.json"
        try:
            if not json_path.exists():
                return
            data = _json.loads(json_path.read_text())
            for op_name, stats_data in data.items():
                try:
                    op_type = OperationType(op_name)
                    stats = self._stats[op_type]
                    stats.total_count = stats_data.get("total_count", 0)
                    stats.success_count = stats_data.get("success_count", 0)
                    stats.timeout_count = stats_data.get("timeout_count", 0)
                except (ValueError, KeyError):
                    continue
        except Exception as e:
            self.log.debug("[AdaptiveTimeout] Failed to load legacy JSON: %s", e)

    # =====================================================================
    # Auto-persist lifecycle
    # =====================================================================

    async def start_auto_persist(self) -> None:
        """Start auto-persist background task."""
        if self._persist_task is not None:
            return

        async def _persist_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(self._persist_interval)
                    await self.persist()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.log.debug("[AdaptiveTimeout] Auto-persist error: %s", e)

        self._persist_task = asyncio.ensure_future(_persist_loop())

    async def stop_auto_persist(self) -> None:
        """Stop auto-persist and do final persist."""
        if self._persist_task is not None:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
            self._persist_task = None
        await self.persist()

    # =====================================================================
    # Visualization
    # =====================================================================

    def visualize(self) -> str:
        lines = [
            "[Adaptive Timeout Manager]",
            f"  System load: {self._load_level.value}",
            f"  Epoch: {self._supervisor_epoch}",
            f"  Enabled: {_is_enabled()}",
            f"  Shadow: {_is_shadow_only()}",
            "",
            "  Operation Statistics:",
        ]
        for op_type, stats in self._stats.items():
            if stats.total_count > 0:
                lines.append(
                    f"    {op_type.value}: "
                    f"mean={stats.get_mean():.0f}ms, "
                    f"p95={stats.get_percentile(95):.0f}ms, "
                    f"success={stats.success_rate:.1%}, "
                    f"n={stats.total_count}"
                    f"{' [cold]' if stats.cold_start else ''}"
                )
        return "\n".join(lines)

    def get_status(self) -> Dict[str, Any]:
        """Status for health endpoints / MCP queries (0.19)."""
        return {
            "enabled": _is_enabled(),
            "shadow_only": _is_shadow_only(),
            "migration_degraded": self._migration_degraded,
            "schema_version": self._schema_version,
            "expected_version": self._expected_schema_version,
            "epoch": self._supervisor_epoch,
            "load_level": self._load_level.value,
            "counters": dict(self._decision_counters),
        }


# =============================================================================
# Singleton (per-process via sys attribute) — Step 1
# =============================================================================

_SYS_ATTR = "_jarvis_adaptive_timeout_manager"


async def get_timeout_manager() -> AdaptiveTimeoutManager:
    """Get or create the per-process singleton timeout manager.

    BOOTSTRAP FAIL-OPEN: If init/load fails, returns None-safe singleton
    that falls through to static defaults.
    """
    existing = getattr(sys, _SYS_ATTR, None)
    if existing is not None:
        return existing

    try:
        mgr = AdaptiveTimeoutManager()
        await mgr.load_stats()
        setattr(sys, _SYS_ATTR, mgr)
        return mgr
    except Exception as e:
        logger.warning("[AdaptiveTimeout] Failed to initialize manager: %s", e)
        # Create bare manager without persistence
        mgr = AdaptiveTimeoutManager()
        setattr(sys, _SYS_ATTR, mgr)
        return mgr


def get_timeout_manager_sync() -> Optional[AdaptiveTimeoutManager]:
    """Get the timeout manager synchronously (may be None).

    Returns None if not yet initialized — callers MUST handle this
    by falling through to static defaults.
    """
    return getattr(sys, _SYS_ATTR, None)


def _reset_timeout_manager() -> None:
    """Reset singleton (for testing only)."""
    if hasattr(sys, _SYS_ATTR):
        delattr(sys, _SYS_ATTR)


# =============================================================================
# Public API: adaptive_get() / adaptive_get_sync() — Step 7
# =============================================================================


async def adaptive_get(
    operation: OperationType,
    default_s: float,
    env_var: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    budget: Optional[TimeoutBudget] = None,
) -> float:
    """
    Get adaptive timeout for an operation. Returns SECONDS.

    Precedence (0.1):
        1. ENV VAR override → always wins
        2. Learned adaptive value (if enabled, not shadow, sufficient data)
        3. Static default (default_s)

    Args:
        operation: The operation type
        default_s: Static default timeout in SECONDS
        env_var: Optional env var name to check for override
        context: Optional context for complexity estimation
        budget: Optional budget to allocate from

    Returns:
        Timeout in seconds
    """
    config = DEFAULT_CONFIGS.get(operation, DEFAULT_CONFIGS[OperationType.GENERIC])
    env_key = env_var or config.env_var
    default_ms = default_s * 1000.0

    # Step 1: ENV override always wins (0.1)
    if env_key:
        env_val = _env_float(env_key, default_s)
        if env_val is not None and os.environ.get(env_key) is not None:
            result_s = env_val
            _log_decision(operation, result_s, DecisionReason.ENV_OVERRIDE, env_key=env_key)
            return _bounds_check_seconds(result_s, default_s)

    # Step 2: Check if adaptive is enabled
    if not _is_enabled():
        _log_decision(operation, default_s, DecisionReason.DISABLED)
        return default_s

    mgr = get_timeout_manager_sync()
    if mgr is None:
        _log_decision(operation, default_s, DecisionReason.BOOTSTRAP_FALLBACK)
        return default_s

    # Step 3: Compute adaptive value
    shadow = _is_shadow_only()
    frozen = mgr._stats_snapshot.get(operation)

    if frozen is None or frozen.total_count == 0:
        adaptive_ms = default_ms
        reason = DecisionReason.INSUFFICIENT_SAMPLES
    else:
        adaptive_ms, reason = mgr._adaptive_timeout_sync(frozen, config)

    # Apply complexity and load
    ctx = context or {}
    complexity = ComplexityEstimator.estimate(operation, ctx)
    adaptive_ms *= complexity
    load_factor = mgr._get_load_factor_sync(config)
    adaptive_ms *= load_factor
    adaptive_ms = max(config.min_ms, min(config.max_ms, adaptive_ms))

    # Budget allocation
    if budget:
        adaptive_ms = budget.allocate(operation.value, adaptive_ms)
        if adaptive_ms <= 0:
            _log_decision(operation, 0.0, DecisionReason.BUDGET_EXHAUSTED)
            return 0.0

    adaptive_s = adaptive_ms / 1000.0

    if shadow:
        # Shadow mode: compute but don't use (0.10)
        delta_s = adaptive_s - default_s
        _log_decision(
            operation, default_s, DecisionReason.SHADOW_MODE,
            shadow_adaptive_s=adaptive_s, shadow_delta_s=delta_s,
        )
        if abs(delta_s) > 0.001:
            mgr._decision_counters["shadow_would_differ"] += 1
        return default_s

    # Use learned value
    _log_decision(operation, adaptive_s, reason)
    return _bounds_check_seconds(adaptive_s, default_s)


def adaptive_get_sync(
    operation: OperationType,
    default_s: float,
    env_var: Optional[str] = None,
) -> float:
    """
    Synchronous adaptive timeout. Returns SECONDS.

    ZERO async. ZERO I/O. Reads from immutable snapshot only.
    Thread-safe without explicit locking.

    Precedence: ENV override → learned (from snapshot) → default.
    """
    config = DEFAULT_CONFIGS.get(operation, DEFAULT_CONFIGS[OperationType.GENERIC])
    env_key = env_var or config.env_var

    # Step 1: ENV override always wins (0.1)
    if env_key:
        env_val = _env_float(env_key, default_s)
        if env_val is not None and os.environ.get(env_key) is not None:
            _log_decision(operation, env_val, DecisionReason.ENV_OVERRIDE, env_key=env_key)
            return _bounds_check_seconds(env_val, default_s)

    # Step 2: Check enabled (uses cached check, no I/O)
    if not _is_enabled():
        _log_decision(operation, default_s, DecisionReason.DISABLED)
        return default_s

    mgr = get_timeout_manager_sync()
    if mgr is None:
        _log_decision(operation, default_s, DecisionReason.BOOTSTRAP_FALLBACK)
        return default_s

    # Step 3: Read from immutable snapshot (no lock needed)
    frozen = mgr._stats_snapshot.get(operation)

    if frozen is None or frozen.total_count == 0:
        if _is_shadow_only():
            _log_decision(operation, default_s, DecisionReason.SHADOW_MODE)
        else:
            _log_decision(operation, default_s, DecisionReason.INSUFFICIENT_SAMPLES)
        return default_s

    adaptive_ms, reason = mgr._adaptive_timeout_sync(frozen, config)
    adaptive_ms = max(config.min_ms, min(config.max_ms, adaptive_ms))
    adaptive_s = adaptive_ms / 1000.0

    if _is_shadow_only():
        delta_s = adaptive_s - default_s
        _log_decision(
            operation, default_s, DecisionReason.SHADOW_MODE,
            shadow_adaptive_s=adaptive_s, shadow_delta_s=delta_s,
        )
        return default_s

    _log_decision(operation, adaptive_s, reason)
    return _bounds_check_seconds(adaptive_s, default_s)


def _bounds_check_seconds(value_s: float, default_s: float) -> float:
    """Bounds check: catch ms/s confusion (0.14)."""
    if value_s > 3600:
        logger.error(
            "[AdaptiveTimeout] Returned value %.1fs exceeds 1 hour — likely ms/s confusion. "
            "Falling back to default %.1fs",
            value_s, default_s,
        )
        return default_s
    return value_s


def _log_decision(
    operation: OperationType,
    value_s: float,
    reason: DecisionReason,
    env_key: Optional[str] = None,
    shadow_adaptive_s: Optional[float] = None,
    shadow_delta_s: Optional[float] = None,
) -> None:
    """Rate-limited telemetry logging (0.15)."""
    mgr = get_timeout_manager_sync()
    if mgr is not None:
        mgr._decision_counters["total"] += 1
        if reason == DecisionReason.ENV_OVERRIDE:
            mgr._decision_counters["source_env"] += 1
        elif reason == DecisionReason.LEARNED_P95:
            mgr._decision_counters["source_learned"] += 1
        elif reason in (
            DecisionReason.DISABLED,
            DecisionReason.COLD_START_DEFAULT,
            DecisionReason.INSUFFICIENT_SAMPLES,
            DecisionReason.BOOTSTRAP_FALLBACK,
            DecisionReason.SHADOW_MODE,
        ):
            mgr._decision_counters["source_default"] += 1

    if not _is_log_decisions():
        return

    # Per-operation debug check
    debug = _is_debug_for_op(operation)

    if not debug:
        # Rate limit: 1 log per operation per 10s
        if mgr is not None:
            key = operation.value
            now = time.time()
            last = mgr._log_rate_limiter.get(key, 0)
            if now - last < mgr._log_rate_limit_interval:
                return
            mgr._log_rate_limiter[key] = now

    parts = [
        f"op={operation.value}",
        f"source={reason.value}",
        f"value={value_s:.3f}s",
    ]
    if env_key:
        parts.append(f"env={env_key}")
    if shadow_adaptive_s is not None:
        parts.append(f"shadow_adaptive={shadow_adaptive_s:.3f}s")
    if shadow_delta_s is not None:
        parts.append(f"shadow_delta={shadow_delta_s:+.3f}s")

    logger.debug("[AdaptiveTimeout] %s", " ".join(parts))


# =============================================================================
# StartupMetricsHistoryAdapter (Step 10)
# =============================================================================

# Controlled mapping — phases with no valid mapping return has()=False (0.8)
_PHASE_MAP: Dict[str, Optional[OperationType]] = {
    "PRE_TRINITY": OperationType.STARTUP_FINALIZATION,
    "TRINITY_PHASE": OperationType.PROCESS_START,
    "GCP_WAIT_BUFFER": OperationType.GCP_VM_STARTUP,
    "POST_TRINITY": OperationType.SERVICE_VERIFICATION,
    "HEALTH_CHECK": OperationType.BACKEND_HEALTH,
    "DISCOVERY": None,  # No direct equivalent
    "CLEANUP": OperationType.SHUTDOWN_CLEANUP,
}


class StartupMetricsHistoryAdapter:
    """
    Bridges AdaptiveTimeoutManager → StartupMetricsHistory protocol.

    Uses controlled mapping (0.8). Unmapped phases fall back to static budgets.
    """

    def __init__(self, manager: AdaptiveTimeoutManager) -> None:
        self._manager = manager

    def has(self, phase: str) -> bool:
        op = _PHASE_MAP.get(phase)
        if op is None:
            return False
        stats = self._manager.get_stats(op)
        return stats.successful_sample_count() >= 5

    def get_p95(self, phase: str) -> Optional[float]:
        op = _PHASE_MAP.get(phase)
        if op is None:
            return None
        stats = self._manager.get_stats(op)
        if stats.successful_sample_count() < 5:
            return None
        p95_ms = stats.get_percentile(95)
        if p95_ms <= 0:
            return None
        return p95_ms / 1000.0  # Convert ms → seconds


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Enums
    "OperationType",
    "TimeoutStrategy",
    "LoadLevel",
    "DecisionReason",
    # Data classes
    "OperationSample",
    "OperationStats",
    "FrozenOperationStats",
    "TimeoutBudget",
    "TimeoutConfig",
    # Complexity
    "ComplexityEstimator",
    # Configuration
    "DEFAULT_CONFIGS",
    # Manager
    "AdaptiveTimeoutManager",
    # Singleton
    "get_timeout_manager",
    "get_timeout_manager_sync",
    # Public API
    "adaptive_get",
    "adaptive_get_sync",
    # Adapter
    "StartupMetricsHistoryAdapter",
]
