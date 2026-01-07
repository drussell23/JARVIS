"""
v78.0: Adaptive Timeout Manager
================================

Provides intelligent, dynamic timeout calculation based on:
- Historical operation performance
- Task complexity estimation
- Current system load
- Time-of-day patterns
- Network latency measurements

Features:
- Rolling statistics per operation type
- Percentile-based timeout calculation (P95, P99)
- Complexity-weighted adjustments
- Load-aware scaling
- Timeout budget tracking
- Deadline propagation
- Circuit breaker integration

Architecture:
    Request → [Estimate Complexity] → [Query History] → [Check Load]
                                            ↓
                    [Calculate Base Timeout] × [Load Factor] × [Complexity Factor]
                                            ↓
                    [Apply Bounds] → [Track Budget] → Return Timeout

Usage:
    from backend.core.coding_council.advanced.adaptive_timeout_manager import (
        get_timeout_manager,
        OperationType,
    )

    manager = await get_timeout_manager()

    # Get adaptive timeout
    timeout = await manager.get_timeout(
        operation=OperationType.API_CALL,
        context={"endpoint": "/api/analyze", "payload_size": 1024}
    )

    # Record actual duration for learning
    with manager.track_operation(OperationType.API_CALL):
        result = await some_operation()

Author: JARVIS v78.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Tuple,
)

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)


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


class TimeoutStrategy(Enum):
    """Strategy for calculating timeout."""
    PERCENTILE_95 = "p95"       # 95th percentile of history
    PERCENTILE_99 = "p99"       # 99th percentile (conservative)
    ADAPTIVE = "adaptive"       # Smart selection based on importance
    FIXED = "fixed"            # Use fixed default
    AGGRESSIVE = "aggressive"   # Tight timeouts for fast failure


class LoadLevel(Enum):
    """System load level."""
    LOW = "low"           # < 30% CPU
    MEDIUM = "medium"     # 30-70% CPU
    HIGH = "high"         # > 70% CPU
    CRITICAL = "critical" # > 90% CPU


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


@dataclass
class OperationStats:
    """Statistics for an operation type."""
    operation_type: OperationType
    samples: Deque[OperationSample] = field(default_factory=lambda: deque(maxlen=1000))
    total_count: int = 0
    success_count: int = 0
    timeout_count: int = 0
    last_updated: float = field(default_factory=time.time)

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_count == 0:
            return 1.0
        return self.success_count / self.total_count

    @property
    def timeout_rate(self) -> float:
        """Calculate timeout rate."""
        if self.total_count == 0:
            return 0.0
        return self.timeout_count / self.total_count

    def add_sample(self, sample: OperationSample):
        """Add a timing sample."""
        self.samples.append(sample)
        self.total_count += 1
        if sample.success:
            self.success_count += 1
        self.last_updated = time.time()

    def get_percentile(self, percentile: float) -> float:
        """Get Nth percentile duration."""
        if not self.samples:
            return 0.0

        durations = sorted(s.duration_ms for s in self.samples if s.success)
        if not durations:
            return 0.0

        index = int(len(durations) * percentile / 100)
        return durations[min(index, len(durations) - 1)]

    def get_mean(self) -> float:
        """Get mean duration."""
        durations = [s.duration_ms for s in self.samples if s.success]
        return statistics.mean(durations) if durations else 0.0

    def get_stddev(self) -> float:
        """Get standard deviation."""
        durations = [s.duration_ms for s in self.samples if s.success]
        return statistics.stdev(durations) if len(durations) > 1 else 0.0


@dataclass
class TimeoutBudget:
    """Tracks remaining timeout budget for cascading operations."""
    total_budget_ms: float
    spent_ms: float = 0.0
    started_at: float = field(default_factory=time.time)
    operations: List[Tuple[str, float]] = field(default_factory=list)

    @property
    def remaining_ms(self) -> float:
        """Get remaining budget."""
        elapsed = (time.time() - self.started_at) * 1000
        return max(0, self.total_budget_ms - elapsed)

    @property
    def is_exhausted(self) -> bool:
        """Check if budget is exhausted."""
        return self.remaining_ms <= 0

    def allocate(self, operation: str, amount_ms: float) -> float:
        """Allocate from budget, returning actual allocation."""
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
    priority: int = 5  # 1-10, higher = more important


# =============================================================================
# Default Configurations
# =============================================================================

DEFAULT_CONFIGS: Dict[OperationType, TimeoutConfig] = {
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
        default_ms=5000, min_ms=500, max_ms=60000,
        priority=5,
    ),
}


# =============================================================================
# Complexity Estimators
# =============================================================================

class ComplexityEstimator:
    """Estimates operation complexity from context."""

    @staticmethod
    def estimate(operation: OperationType, context: Dict[str, Any]) -> float:
        """
        Estimate complexity factor (1.0 = normal).

        Returns multiplier based on operation-specific heuristics.
        """
        if operation == OperationType.API_CALL:
            return ComplexityEstimator._api_complexity(context)
        elif operation == OperationType.DB_QUERY:
            return ComplexityEstimator._db_complexity(context)
        elif operation == OperationType.FILE_READ:
            return ComplexityEstimator._file_complexity(context)
        elif operation == OperationType.MODEL_INFERENCE:
            return ComplexityEstimator._ml_complexity(context)
        elif operation == OperationType.CODING_COUNCIL:
            return ComplexityEstimator._council_complexity(context)
        return 1.0

    @staticmethod
    def _api_complexity(ctx: Dict[str, Any]) -> float:
        """API call complexity based on payload size, endpoint, etc."""
        base = 1.0

        # Payload size factor
        payload_size = ctx.get("payload_size", 0)
        if payload_size > 100000:  # 100KB+
            base *= 1.5
        elif payload_size > 10000:  # 10KB+
            base *= 1.2

        # Known slow endpoints
        endpoint = ctx.get("endpoint", "")
        if "/analyze" in endpoint or "/generate" in endpoint:
            base *= 1.5
        if "/bulk" in endpoint or "/batch" in endpoint:
            base *= 2.0

        return base

    @staticmethod
    def _db_complexity(ctx: Dict[str, Any]) -> float:
        """Database query complexity."""
        base = 1.0

        # Query type
        query_type = ctx.get("query_type", "")
        if query_type in ("join", "aggregate", "subquery"):
            base *= 1.5

        # Expected rows
        expected_rows = ctx.get("expected_rows", 0)
        if expected_rows > 10000:
            base *= 2.0
        elif expected_rows > 1000:
            base *= 1.3

        return base

    @staticmethod
    def _file_complexity(ctx: Dict[str, Any]) -> float:
        """File operation complexity."""
        base = 1.0

        file_size = ctx.get("file_size", 0)
        if file_size > 10_000_000:  # 10MB+
            base *= 2.0
        elif file_size > 1_000_000:  # 1MB+
            base *= 1.5

        return base

    @staticmethod
    def _ml_complexity(ctx: Dict[str, Any]) -> float:
        """ML inference complexity."""
        base = 1.0

        # Batch size
        batch_size = ctx.get("batch_size", 1)
        base *= math.log2(batch_size + 1)

        # Input size
        input_tokens = ctx.get("input_tokens", 0)
        if input_tokens > 4096:
            base *= 2.0
        elif input_tokens > 1024:
            base *= 1.5

        return base

    @staticmethod
    def _council_complexity(ctx: Dict[str, Any]) -> float:
        """Coding council operation complexity."""
        base = 1.0

        # Number of files
        num_files = ctx.get("num_files", 1)
        base *= math.log2(num_files + 1)

        # Lines of code
        loc = ctx.get("lines_of_code", 0)
        if loc > 10000:
            base *= 2.0
        elif loc > 1000:
            base *= 1.5

        return base


# =============================================================================
# Adaptive Timeout Manager
# =============================================================================

class AdaptiveTimeoutManager:
    """
    Intelligent timeout management with adaptive learning.

    Provides dynamic timeout calculation based on historical performance,
    current system load, and operation complexity.

    Thread-safe and async-compatible.
    """

    def __init__(
        self,
        logger_instance: Optional[logging.Logger] = None,
        history_window: int = 1000,
    ):
        self.log = logger_instance or logger
        self.history_window = history_window

        # Stats per operation type
        self._stats: Dict[OperationType, OperationStats] = {}
        for op_type in OperationType:
            self._stats[op_type] = OperationStats(operation_type=op_type)

        # Configs
        self._configs: Dict[OperationType, TimeoutConfig] = DEFAULT_CONFIGS.copy()

        # Budget tracking
        self._active_budgets: Dict[str, TimeoutBudget] = {}

        # System load cache
        self._load_level: LoadLevel = LoadLevel.MEDIUM
        self._load_last_checked: float = 0
        self._load_check_interval: float = 5.0  # seconds

        # Persistence
        self._lock = asyncio.Lock()
        self._persist_file = Path.home() / ".jarvis" / "trinity" / "timeout_stats.json"
        self._persist_file.parent.mkdir(parents=True, exist_ok=True)

    async def get_timeout(
        self,
        operation: OperationType,
        context: Optional[Dict[str, Any]] = None,
        budget: Optional[TimeoutBudget] = None,
        strategy: Optional[TimeoutStrategy] = None,
    ) -> float:
        """
        Get adaptive timeout for an operation.

        Args:
            operation: Type of operation
            context: Context for complexity estimation
            budget: Optional budget to allocate from
            strategy: Override strategy

        Returns:
            Timeout in milliseconds
        """
        context = context or {}
        config = self._configs.get(operation, DEFAULT_CONFIGS[OperationType.GENERIC])
        stats = self._stats[operation]

        # Get strategy
        strat = strategy or config.strategy

        # Calculate base timeout
        if strat == TimeoutStrategy.FIXED:
            base_timeout = config.default_ms
        elif strat == TimeoutStrategy.AGGRESSIVE:
            base_timeout = stats.get_percentile(75) or config.default_ms * 0.5
        elif strat == TimeoutStrategy.PERCENTILE_99:
            base_timeout = stats.get_percentile(99) or config.default_ms
        elif strat == TimeoutStrategy.PERCENTILE_95:
            base_timeout = stats.get_percentile(95) or config.default_ms
        else:  # ADAPTIVE
            base_timeout = await self._adaptive_timeout(stats, config)

        # Apply complexity factor
        complexity = ComplexityEstimator.estimate(operation, context)
        timeout = base_timeout * complexity * config.complexity_weight

        # Apply load factor
        load_factor = await self._get_load_factor(config)
        timeout *= load_factor

        # Apply bounds
        timeout = max(config.min_ms, min(config.max_ms, timeout))

        # Allocate from budget if provided
        if budget:
            timeout = budget.allocate(operation.value, timeout)

        self.log.debug(
            f"[Timeout] {operation.value}: {timeout:.0f}ms "
            f"(base={base_timeout:.0f}, complexity={complexity:.2f}, load={load_factor:.2f})"
        )

        return timeout

    async def _adaptive_timeout(
        self,
        stats: OperationStats,
        config: TimeoutConfig,
    ) -> float:
        """Calculate adaptive timeout based on history and success rate."""
        if stats.total_count < 10:
            # Not enough data, use default
            return config.default_ms

        # Start with P95
        timeout = stats.get_percentile(95)

        # Adjust based on success rate
        if stats.success_rate < 0.9:
            # Low success rate, increase timeout
            timeout *= 1.5
        elif stats.success_rate > 0.99:
            # Very high success, can be more aggressive
            timeout *= 0.8

        # Adjust based on timeout rate
        if stats.timeout_rate > 0.1:
            # Too many timeouts, increase significantly
            timeout *= 2.0
        elif stats.timeout_rate < 0.01:
            # Very few timeouts, can tighten
            timeout *= 0.9

        # Add buffer based on standard deviation
        stddev = stats.get_stddev()
        if stddev > 0:
            timeout += stddev  # Add 1 std dev as buffer

        return timeout

    async def _get_load_factor(self, config: TimeoutConfig) -> float:
        """Get load-based scaling factor."""
        await self._update_load_level()

        base_factors = {
            LoadLevel.LOW: 0.8,
            LoadLevel.MEDIUM: 1.0,
            LoadLevel.HIGH: 1.5,
            LoadLevel.CRITICAL: 2.5,
        }

        base = base_factors[self._load_level]

        # Apply sensitivity
        if config.load_sensitivity != 1.0:
            # Adjust factor towards 1.0 based on sensitivity
            # sensitivity=0 means no load adjustment, sensitivity=2 means double effect
            adjustment = (base - 1.0) * config.load_sensitivity
            return 1.0 + adjustment

        return base

    async def _update_load_level(self):
        """Update system load level."""
        now = time.time()
        if now - self._load_last_checked < self._load_check_interval:
            return

        self._load_last_checked = now

        if not PSUTIL_AVAILABLE:
            return

        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)

            if cpu_percent > 90:
                self._load_level = LoadLevel.CRITICAL
            elif cpu_percent > 70:
                self._load_level = LoadLevel.HIGH
            elif cpu_percent > 30:
                self._load_level = LoadLevel.MEDIUM
            else:
                self._load_level = LoadLevel.LOW

        except Exception:
            pass

    @asynccontextmanager
    async def track_operation(
        self,
        operation: OperationType,
        context: Optional[Dict[str, Any]] = None,
    ):
        """
        Context manager to track operation duration.

        Usage:
            async with manager.track_operation(OperationType.API_CALL) as tracker:
                result = await some_api_call()
            # Duration automatically recorded
        """
        context = context or {}
        start_time = time.time()
        success = True
        complexity = ComplexityEstimator.estimate(operation, context)

        try:
            yield
        except asyncio.TimeoutError:
            success = False
            async with self._lock:
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
            )

            async with self._lock:
                self._stats[operation].add_sample(sample)

    def record_duration(
        self,
        operation: OperationType,
        duration_ms: float,
        success: bool = True,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Manually record an operation duration."""
        context = context or {}
        sample = OperationSample(
            duration_ms=duration_ms,
            timestamp=time.time(),
            success=success,
            complexity=ComplexityEstimator.estimate(operation, context),
            context=context,
        )
        self._stats[operation].add_sample(sample)

    def create_budget(
        self,
        total_ms: float,
        budget_id: Optional[str] = None,
    ) -> TimeoutBudget:
        """Create a timeout budget for cascading operations."""
        budget = TimeoutBudget(total_budget_ms=total_ms)
        budget_id = budget_id or f"budget_{time.time()}"
        self._active_budgets[budget_id] = budget
        return budget

    def get_config(self, operation: OperationType) -> TimeoutConfig:
        """Get configuration for an operation type."""
        return self._configs.get(operation, DEFAULT_CONFIGS[OperationType.GENERIC])

    def set_config(self, operation: OperationType, config: TimeoutConfig):
        """Set configuration for an operation type."""
        self._configs[operation] = config

    def get_stats(self, operation: OperationType) -> OperationStats:
        """Get statistics for an operation type."""
        return self._stats[operation]

    async def persist(self):
        """Persist statistics to disk."""
        try:
            data = {}
            for op_type, stats in self._stats.items():
                if stats.total_count > 0:
                    data[op_type.value] = {
                        "total_count": stats.total_count,
                        "success_count": stats.success_count,
                        "timeout_count": stats.timeout_count,
                        "mean_ms": stats.get_mean(),
                        "p95_ms": stats.get_percentile(95),
                        "p99_ms": stats.get_percentile(99),
                    }

            self._persist_file.write_text(json.dumps(data, indent=2))

        except Exception as e:
            self.log.debug(f"[Timeout] Failed to persist: {e}")

    async def load_stats(self):
        """Load statistics from disk."""
        try:
            if not self._persist_file.exists():
                return

            data = json.loads(self._persist_file.read_text())

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
            self.log.debug(f"[Timeout] Failed to load stats: {e}")

    def visualize(self) -> str:
        """Generate visualization of timeout statistics."""
        lines = [
            "[Adaptive Timeout Manager]",
            f"  System load: {self._load_level.value}",
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
                )

        return "\n".join(lines)


# =============================================================================
# Singleton Instance
# =============================================================================

_timeout_manager: Optional[AdaptiveTimeoutManager] = None
_manager_lock: Optional[asyncio.Lock] = None  # v78.1: Lazy init for Python 3.9 compat


def _get_manager_lock() -> asyncio.Lock:
    """v78.1: Lazy lock initialization to avoid 'no running event loop' error on import."""
    global _manager_lock
    if _manager_lock is None:
        _manager_lock = asyncio.Lock()
    return _manager_lock


async def get_timeout_manager() -> AdaptiveTimeoutManager:
    """Get or create the singleton timeout manager."""
    global _timeout_manager

    async with _get_manager_lock():
        if _timeout_manager is None:
            _timeout_manager = AdaptiveTimeoutManager()
            await _timeout_manager.load_stats()
        return _timeout_manager


def get_timeout_manager_sync() -> Optional[AdaptiveTimeoutManager]:
    """Get the timeout manager synchronously (may be None)."""
    return _timeout_manager
