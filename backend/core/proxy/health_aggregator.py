"""
Unified Health Aggregator - Layer 4 of Distributed Proxy System

Provides:
- Cross-repo health coordination
- Real-time metrics collection (latency, error rate, pool status)
- Anomaly detection with Z-score analysis
- Event sourcing for complete audit trail
- Automated diagnosis around failures

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Deque,
    Dict,
    Final,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
)

if TYPE_CHECKING:
    from .lifecycle_controller import ProxyLifecycleController, ProxyState

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class HealthConfig:
    """Configuration loaded from environment variables."""

    # Sliding window statistics
    WINDOW_SIZE: Final[int] = int(os.getenv("HEALTH_WINDOW_SIZE", "100"))
    BASELINE_MIN_SAMPLES: Final[int] = int(os.getenv("BASELINE_MIN_SAMPLES", "20"))

    # Anomaly detection
    ZSCORE_THRESHOLD: Final[float] = float(os.getenv("ANOMALY_ZSCORE_THRESHOLD", "2.5"))
    ANOMALY_COOLDOWN: Final[float] = float(os.getenv("ANOMALY_COOLDOWN_SECONDS", "60.0"))

    # Diagnosis
    DIAGNOSIS_WINDOW_SECONDS: Final[float] = float(os.getenv("DIAGNOSIS_WINDOW_SECONDS", "300.0"))

    # Event sourcing
    EVENT_LOG_PATH: Final[Path] = Path(
        os.getenv("HEALTH_EVENT_LOG", str(Path.home() / ".jarvis" / "health_events.jsonl"))
    )
    EVENT_LOG_MAX_SIZE_MB: Final[int] = int(os.getenv("HEALTH_EVENT_LOG_MAX_MB", "50"))
    EVENT_LOG_ROTATE_COUNT: Final[int] = int(os.getenv("HEALTH_EVENT_LOG_ROTATE", "5"))

    # Collection interval
    COLLECTION_INTERVAL: Final[float] = float(os.getenv("HEALTH_COLLECTION_INTERVAL", "5.0"))

    # Cross-repo state
    STATE_DIR: Final[Path] = Path(os.getenv("JARVIS_STATE_DIR", str(Path.home() / ".jarvis")))
    CROSS_REPO_STATE_FILE: Final[Path] = STATE_DIR / "cross_repo" / "unified_state.json"


# =============================================================================
# Anomaly Types
# =============================================================================

class AnomalyType(Enum):
    """Types of detected anomalies."""
    LATENCY_SPIKE = auto()
    LATENCY_DEGRADATION = auto()
    ERROR_RATE_SPIKE = auto()
    CONNECTION_POOL_EXHAUSTION = auto()
    PROXY_STATE_CHANGE = auto()
    CONSECUTIVE_FAILURES = auto()
    SLEEP_WAKE_RECOVERY = auto()
    PROCESS_RESTART = auto()


# =============================================================================
# Health Snapshot
# =============================================================================

@dataclass
class HealthSnapshot:
    """
    Point-in-time health snapshot with all metrics.

    Used for both real-time monitoring and historical analysis.
    """
    # Identification
    timestamp: float
    correlation_id: str
    source_repo: str

    # Proxy state
    proxy_state: str  # ProxyState name
    proxy_pid: Optional[int]
    proxy_uptime_seconds: float

    # Connection latencies
    tcp_latency_ms: float
    db_latency_ms: float
    tls_latency_ms: float

    # Connection pool (if available)
    pool_size: int = 0
    pool_available: int = 0
    pool_waiting: int = 0
    pool_min: int = 0
    pool_max: int = 0

    # Error tracking
    error_count: int = 0
    consecutive_failures: int = 0

    # Anomaly indicators
    latency_zscore: float = 0.0
    is_anomaly: bool = False
    anomaly_type: Optional[str] = None
    anomaly_details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "timestamp": self.timestamp,
            "timestamp_iso": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "correlation_id": self.correlation_id,
            "source_repo": self.source_repo,
            "proxy_state": self.proxy_state,
            "proxy_pid": self.proxy_pid,
            "proxy_uptime_seconds": round(self.proxy_uptime_seconds, 2),
            "tcp_latency_ms": round(self.tcp_latency_ms, 2),
            "db_latency_ms": round(self.db_latency_ms, 2),
            "tls_latency_ms": round(self.tls_latency_ms, 2),
            "pool_size": self.pool_size,
            "pool_available": self.pool_available,
            "pool_waiting": self.pool_waiting,
            "error_count": self.error_count,
            "consecutive_failures": self.consecutive_failures,
            "latency_zscore": round(self.latency_zscore, 3),
            "is_anomaly": self.is_anomaly,
            "anomaly_type": self.anomaly_type,
            "anomaly_details": self.anomaly_details,
        }


# =============================================================================
# Health Event (Event Sourcing)
# =============================================================================

class EventType(Enum):
    """Types of health events for audit trail."""
    SNAPSHOT = "snapshot"
    STATE_CHANGE = "state_change"
    ANOMALY_DETECTED = "anomaly_detected"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    DIAGNOSIS = "diagnosis"
    ERROR = "error"
    CONFIG_CHANGE = "config_change"


@dataclass
class HealthEvent:
    """
    Immutable health event for audit trail.

    Events are append-only and never modified, providing
    a complete historical record for debugging.
    """
    event_id: str
    event_type: EventType
    timestamp: float
    correlation_id: str
    source_repo: str
    data: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to JSON line."""
        return json.dumps({
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "timestamp_iso": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "correlation_id": self.correlation_id,
            "source_repo": self.source_repo,
            "data": self.data,
            "metadata": self.metadata,
        })

    @classmethod
    def from_json(cls, json_line: str) -> HealthEvent:
        """Deserialize from JSON line."""
        data = json.loads(json_line)
        return cls(
            event_id=data["event_id"],
            event_type=EventType(data["event_type"]),
            timestamp=data["timestamp"],
            correlation_id=data["correlation_id"],
            source_repo=data["source_repo"],
            data=data["data"],
            metadata=data.get("metadata", {}),
        )


# =============================================================================
# Sliding Window Statistics
# =============================================================================

class SlidingWindowStats:
    """
    Maintains rolling statistics for anomaly detection.

    Uses robust estimators (median, MAD) to reduce outlier impact
    while tracking standard statistics for Z-score calculation.
    """

    def __init__(self, window_size: int = HealthConfig.WINDOW_SIZE):
        self._window_size = window_size
        self._values: Deque[float] = deque(maxlen=window_size)
        self._timestamps: Deque[float] = deque(maxlen=window_size)

        # Cached statistics (updated on demand)
        self._cache_valid = False
        self._cached_mean: float = 0.0
        self._cached_std: float = 0.0
        self._cached_median: float = 0.0
        self._cached_mad: float = 0.0  # Median Absolute Deviation

    def add(self, value: float, timestamp: Optional[float] = None) -> None:
        """Add a value to the window."""
        self._values.append(value)
        self._timestamps.append(timestamp or time.time())
        self._cache_valid = False

    def _update_cache(self) -> None:
        """Update cached statistics."""
        if self._cache_valid or len(self._values) == 0:
            return

        values = list(self._values)

        # Standard statistics
        self._cached_mean = statistics.mean(values)
        self._cached_std = statistics.stdev(values) if len(values) > 1 else 0.0

        # Robust statistics
        self._cached_median = statistics.median(values)

        # MAD: median(|x - median|) * 1.4826 (scale factor for normal distribution)
        deviations = [abs(v - self._cached_median) for v in values]
        mad_raw = statistics.median(deviations) if deviations else 0.0
        self._cached_mad = mad_raw * 1.4826

        self._cache_valid = True

    @property
    def count(self) -> int:
        """Number of samples in window."""
        return len(self._values)

    @property
    def mean(self) -> float:
        """Mean of values in window."""
        self._update_cache()
        return self._cached_mean

    @property
    def std(self) -> float:
        """Standard deviation of values."""
        self._update_cache()
        return self._cached_std

    @property
    def median(self) -> float:
        """Median of values (robust center)."""
        self._update_cache()
        return self._cached_median

    @property
    def mad(self) -> float:
        """Median Absolute Deviation (robust spread)."""
        self._update_cache()
        return self._cached_mad

    def zscore(self, value: float) -> float:
        """Calculate Z-score for a value."""
        self._update_cache()
        if self._cached_std == 0:
            return 0.0
        return (value - self._cached_mean) / self._cached_std

    def robust_zscore(self, value: float) -> float:
        """Calculate robust Z-score using MAD."""
        self._update_cache()
        if self._cached_mad == 0:
            return 0.0
        return (value - self._cached_median) / self._cached_mad

    def is_anomaly(
        self,
        value: float,
        threshold: float = HealthConfig.ZSCORE_THRESHOLD,
        use_robust: bool = True,
    ) -> Tuple[bool, float]:
        """
        Check if a value is anomalous.

        Returns (is_anomaly, zscore).
        """
        if self.count < HealthConfig.BASELINE_MIN_SAMPLES:
            return (False, 0.0)

        if use_robust:
            z = self.robust_zscore(value)
        else:
            z = self.zscore(value)

        return (abs(z) > threshold, z)

    def get_percentile(self, percentile: float) -> float:
        """Get value at given percentile (0-100)."""
        if not self._values:
            return 0.0
        sorted_values = sorted(self._values)
        idx = int(len(sorted_values) * percentile / 100)
        idx = min(idx, len(sorted_values) - 1)
        return sorted_values[idx]

    def get_stats(self) -> Dict[str, float]:
        """Get all current statistics."""
        self._update_cache()
        return {
            "count": self.count,
            "mean": round(self.mean, 3),
            "std": round(self.std, 3),
            "median": round(self.median, 3),
            "mad": round(self.mad, 3),
            "min": round(min(self._values), 3) if self._values else 0.0,
            "max": round(max(self._values), 3) if self._values else 0.0,
            "p95": round(self.get_percentile(95), 3),
            "p99": round(self.get_percentile(99), 3),
        }


# =============================================================================
# Event Store (Append-Only Log)
# =============================================================================

class EventStore:
    """
    Append-only event store with automatic rotation.

    Provides:
    - JSONL format for easy processing
    - Automatic file rotation on size threshold
    - Query API for debugging
    """

    def __init__(
        self,
        log_path: Path = HealthConfig.EVENT_LOG_PATH,
        max_size_mb: int = HealthConfig.EVENT_LOG_MAX_SIZE_MB,
        rotate_count: int = HealthConfig.EVENT_LOG_ROTATE_COUNT,
    ):
        self._log_path = log_path
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._rotate_count = rotate_count
        self._lock = asyncio.Lock()

        # Ensure directory exists
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, event: HealthEvent) -> None:
        """Append an event to the log."""
        async with self._lock:
            # Check if rotation needed
            if self._log_path.exists():
                if self._log_path.stat().st_size > self._max_size_bytes:
                    await self._rotate()

            # Append event
            with open(self._log_path, "a") as f:
                f.write(event.to_json() + "\n")

    async def _rotate(self) -> None:
        """Rotate log files."""
        # Shift existing rotations
        for i in range(self._rotate_count - 1, 0, -1):
            old_path = self._log_path.with_suffix(f".{i}.jsonl")
            new_path = self._log_path.with_suffix(f".{i + 1}.jsonl")
            if old_path.exists():
                old_path.replace(new_path)

        # Move current to .1
        if self._log_path.exists():
            self._log_path.replace(self._log_path.with_suffix(".1.jsonl"))

        logger.info(f"[EventStore] Rotated log files")

    async def query(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        event_types: Optional[Set[EventType]] = None,
        correlation_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[HealthEvent]:
        """
        Query events from the log.

        Args:
            start_time: Filter events after this timestamp
            end_time: Filter events before this timestamp
            event_types: Filter by event types
            correlation_id: Filter by correlation ID
            limit: Maximum events to return

        Returns:
            List of matching events (newest first)
        """
        events: List[HealthEvent] = []

        if not self._log_path.exists():
            return events

        try:
            with open(self._log_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = HealthEvent.from_json(line)

                        # Apply filters
                        if start_time and event.timestamp < start_time:
                            continue
                        if end_time and event.timestamp > end_time:
                            continue
                        if event_types and event.event_type not in event_types:
                            continue
                        if correlation_id and event.correlation_id != correlation_id:
                            continue

                        events.append(event)

                    except (json.JSONDecodeError, KeyError):
                        continue

            # Return newest first, limited
            events.sort(key=lambda e: e.timestamp, reverse=True)
            return events[:limit]

        except Exception as e:
            logger.error(f"[EventStore] Query error: {e}")
            return []

    async def get_events_around(
        self,
        timestamp: float,
        window_seconds: float = 60.0,
        limit: int = 100,
    ) -> List[HealthEvent]:
        """Get events around a specific timestamp."""
        return await self.query(
            start_time=timestamp - window_seconds,
            end_time=timestamp + window_seconds,
            limit=limit,
        )


# =============================================================================
# Anomaly Detector
# =============================================================================

class AnomalyDetector:
    """
    Detects anomalies in health metrics using statistical analysis.

    Features:
    - Z-score based detection (both standard and robust)
    - Cooldown to prevent alert fatigue
    - Pattern recognition for common issues
    """

    def __init__(self):
        # Sliding windows for different metrics
        self._latency_stats = SlidingWindowStats()
        self._error_rate_stats = SlidingWindowStats(window_size=50)

        # Cooldown tracking
        self._last_anomaly_time: Dict[AnomalyType, float] = {}

        # Anomaly history
        self._anomaly_history: Deque[Tuple[float, AnomalyType, Dict[str, Any]]] = deque(
            maxlen=100
        )

    def record_latency(self, latency_ms: float) -> None:
        """Record a latency measurement."""
        self._latency_stats.add(latency_ms)

    def record_error(self, is_error: bool) -> None:
        """Record an error occurrence (1 for error, 0 for success)."""
        self._error_rate_stats.add(1.0 if is_error else 0.0)

    def _is_in_cooldown(self, anomaly_type: AnomalyType) -> bool:
        """Check if anomaly type is in cooldown period."""
        last_time = self._last_anomaly_time.get(anomaly_type, 0)
        return (time.time() - last_time) < HealthConfig.ANOMALY_COOLDOWN

    def _record_anomaly(
        self,
        anomaly_type: AnomalyType,
        details: Dict[str, Any],
    ) -> None:
        """Record an anomaly occurrence."""
        now = time.time()
        self._last_anomaly_time[anomaly_type] = now
        self._anomaly_history.append((now, anomaly_type, details))

    def detect(self, snapshot: HealthSnapshot) -> Tuple[bool, Optional[AnomalyType], Dict[str, Any]]:
        """
        Detect anomalies in a health snapshot.

        Returns (is_anomaly, anomaly_type, details).
        """
        # Record metrics
        self.record_latency(snapshot.tcp_latency_ms)
        self.record_error(snapshot.error_count > 0)

        # Check for various anomaly types
        anomalies: List[Tuple[AnomalyType, Dict[str, Any]]] = []

        # 1. Latency spike
        is_latency_anomaly, zscore = self._latency_stats.is_anomaly(snapshot.tcp_latency_ms)
        if is_latency_anomaly and not self._is_in_cooldown(AnomalyType.LATENCY_SPIKE):
            if zscore > 0:  # Only positive spikes
                anomalies.append((
                    AnomalyType.LATENCY_SPIKE,
                    {
                        "latency_ms": snapshot.tcp_latency_ms,
                        "zscore": zscore,
                        "baseline_median": self._latency_stats.median,
                        "threshold": self._latency_stats.median * HealthConfig.ZSCORE_THRESHOLD,
                    }
                ))

        # 2. Consecutive failures
        if snapshot.consecutive_failures >= 3 and not self._is_in_cooldown(AnomalyType.CONSECUTIVE_FAILURES):
            anomalies.append((
                AnomalyType.CONSECUTIVE_FAILURES,
                {
                    "count": snapshot.consecutive_failures,
                    "last_error": snapshot.anomaly_details.get("last_error", "unknown"),
                }
            ))

        # 3. Connection pool exhaustion
        if snapshot.pool_size > 0:
            pool_usage = (snapshot.pool_size - snapshot.pool_available) / snapshot.pool_size
            if pool_usage > 0.9 and not self._is_in_cooldown(AnomalyType.CONNECTION_POOL_EXHAUSTION):
                anomalies.append((
                    AnomalyType.CONNECTION_POOL_EXHAUSTION,
                    {
                        "usage_percent": pool_usage * 100,
                        "pool_size": snapshot.pool_size,
                        "available": snapshot.pool_available,
                        "waiting": snapshot.pool_waiting,
                    }
                ))

        # Return highest priority anomaly
        if anomalies:
            anomaly_type, details = anomalies[0]
            self._record_anomaly(anomaly_type, details)
            return (True, anomaly_type, details)

        return (False, None, {})

    def get_latency_stats(self) -> Dict[str, float]:
        """Get current latency statistics."""
        return self._latency_stats.get_stats()

    def get_recent_anomalies(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent anomalies."""
        anomalies = list(self._anomaly_history)[-limit:]
        return [
            {
                "timestamp": ts,
                "type": atype.name,
                "details": details,
            }
            for ts, atype, details in anomalies
        ]


# =============================================================================
# Diagnosis Engine
# =============================================================================

class DiagnosisEngine:
    """
    Automated diagnosis of health issues.

    Analyzes patterns around failures to provide actionable insights.
    """

    def __init__(self, event_store: EventStore):
        self._event_store = event_store

    async def diagnose_failure(
        self,
        failure_time: float,
        correlation_id: str,
    ) -> Dict[str, Any]:
        """
        Diagnose a failure by analyzing surrounding events.

        Args:
            failure_time: Timestamp of the failure
            correlation_id: Correlation ID for related events

        Returns:
            Diagnosis report with probable causes and recommendations
        """
        window_seconds = HealthConfig.DIAGNOSIS_WINDOW_SECONDS

        # Get events around failure
        events = await self._event_store.get_events_around(
            failure_time,
            window_seconds=window_seconds,
        )

        if not events:
            return {
                "status": "insufficient_data",
                "message": "No events found around failure time",
            }

        # Analyze patterns
        diagnosis = {
            "failure_time": failure_time,
            "failure_time_iso": datetime.fromtimestamp(
                failure_time, tz=timezone.utc
            ).isoformat(),
            "correlation_id": correlation_id,
            "analysis_window_seconds": window_seconds,
            "events_analyzed": len(events),
            "probable_causes": [],
            "recommendations": [],
            "timeline": [],
        }

        # Build timeline
        for event in sorted(events, key=lambda e: e.timestamp):
            relative_time = event.timestamp - failure_time
            diagnosis["timeline"].append({
                "relative_seconds": round(relative_time, 2),
                "event_type": event.event_type.value,
                "summary": self._summarize_event(event),
            })

        # Analyze for patterns
        self._analyze_latency_degradation(events, diagnosis)
        self._analyze_state_changes(events, diagnosis)
        self._analyze_error_patterns(events, diagnosis)

        # Generate recommendations
        self._generate_recommendations(diagnosis)

        return diagnosis

    def _summarize_event(self, event: HealthEvent) -> str:
        """Create a brief summary of an event."""
        if event.event_type == EventType.SNAPSHOT:
            data = event.data
            return f"Health check: {data.get('proxy_state', 'unknown')}, latency={data.get('tcp_latency_ms', 0):.1f}ms"
        elif event.event_type == EventType.STATE_CHANGE:
            return f"State: {event.data.get('from_state', '?')} → {event.data.get('to_state', '?')}"
        elif event.event_type == EventType.ANOMALY_DETECTED:
            return f"Anomaly: {event.data.get('anomaly_type', 'unknown')}"
        elif event.event_type == EventType.ERROR:
            return f"Error: {event.data.get('message', 'unknown')[:50]}"
        else:
            return event.event_type.value

    def _analyze_latency_degradation(
        self,
        events: List[HealthEvent],
        diagnosis: Dict[str, Any],
    ) -> None:
        """Analyze for latency degradation patterns."""
        latencies = []
        for event in events:
            if event.event_type == EventType.SNAPSHOT:
                latency = event.data.get("tcp_latency_ms", 0)
                if latency > 0:
                    latencies.append((event.timestamp, latency))

        if len(latencies) < 5:
            return

        # Check for upward trend
        latencies.sort(key=lambda x: x[0])
        recent = latencies[-5:]
        older = latencies[:5]

        recent_avg = statistics.mean([l[1] for l in recent])
        older_avg = statistics.mean([l[1] for l in older])

        if recent_avg > older_avg * 2:
            diagnosis["probable_causes"].append({
                "type": "latency_degradation",
                "confidence": "high",
                "description": f"Latency increased from avg {older_avg:.1f}ms to {recent_avg:.1f}ms before failure",
            })

    def _analyze_state_changes(
        self,
        events: List[HealthEvent],
        diagnosis: Dict[str, Any],
    ) -> None:
        """Analyze proxy state change patterns."""
        state_changes = [
            e for e in events
            if e.event_type == EventType.STATE_CHANGE
        ]

        if not state_changes:
            return

        # Check for rapid state changes (instability)
        if len(state_changes) >= 3:
            # Calculate time span
            times = [e.timestamp for e in state_changes]
            span = max(times) - min(times)
            if span > 0 and len(state_changes) / span > 0.1:  # More than 1 per 10 seconds
                diagnosis["probable_causes"].append({
                    "type": "proxy_instability",
                    "confidence": "high",
                    "description": f"{len(state_changes)} state changes in {span:.0f} seconds indicates instability",
                })

    def _analyze_error_patterns(
        self,
        events: List[HealthEvent],
        diagnosis: Dict[str, Any],
    ) -> None:
        """Analyze error event patterns."""
        errors = [e for e in events if e.event_type == EventType.ERROR]

        if not errors:
            return

        # Group by error type
        error_types: Dict[str, int] = {}
        for error in errors:
            error_msg = str(error.data.get("message", "unknown"))
            # Extract error type from message
            if "connection refused" in error_msg.lower():
                error_type = "connection_refused"
            elif "timeout" in error_msg.lower():
                error_type = "timeout"
            elif "authentication" in error_msg.lower():
                error_type = "authentication"
            else:
                error_type = "other"

            error_types[error_type] = error_types.get(error_type, 0) + 1

        for error_type, count in error_types.items():
            if count >= 2:
                diagnosis["probable_causes"].append({
                    "type": f"repeated_{error_type}",
                    "confidence": "medium",
                    "description": f"{count} occurrences of {error_type} errors",
                })

    def _generate_recommendations(self, diagnosis: Dict[str, Any]) -> None:
        """Generate recommendations based on analysis."""
        causes = diagnosis["probable_causes"]

        for cause in causes:
            cause_type = cause["type"]

            if cause_type == "latency_degradation":
                diagnosis["recommendations"].append(
                    "Consider preemptive restart when latency trends upward"
                )
                diagnosis["recommendations"].append(
                    "Check Cloud SQL instance CPU and memory utilization"
                )

            elif cause_type == "proxy_instability":
                diagnosis["recommendations"].append(
                    "Increase proxy restart throttle interval"
                )
                diagnosis["recommendations"].append(
                    "Check for sleep/wake cycles causing reconnection storms"
                )

            elif cause_type == "repeated_connection_refused":
                diagnosis["recommendations"].append(
                    "Verify Cloud SQL proxy is running and healthy"
                )
                diagnosis["recommendations"].append(
                    "Check if proxy port is blocked by firewall"
                )

            elif cause_type == "repeated_timeout":
                diagnosis["recommendations"].append(
                    "Increase connection timeout values"
                )
                diagnosis["recommendations"].append(
                    "Check network connectivity to Cloud SQL"
                )

        # Deduplicate recommendations
        diagnosis["recommendations"] = list(set(diagnosis["recommendations"]))


# =============================================================================
# Unified Health Aggregator
# =============================================================================

class UnifiedHealthAggregator:
    """
    Main health aggregator coordinating all health monitoring.

    Features:
    - Real-time metric collection
    - Cross-repo state synchronization
    - Anomaly detection and alerting
    - Event sourcing for audit trails
    - Automated diagnosis
    """

    def __init__(
        self,
        source_repo: str = "jarvis",
        lifecycle_controller: Optional[ProxyLifecycleController] = None,
    ):
        self._source_repo = source_repo
        self._lifecycle = lifecycle_controller
        self._correlation_id = str(uuid.uuid4())[:8]

        # Components
        self._event_store = EventStore()
        self._anomaly_detector = AnomalyDetector()
        self._diagnosis_engine = DiagnosisEngine(self._event_store)

        # State
        self._running = False
        self._collection_task: Optional[asyncio.Task[None]] = None
        self._last_snapshot: Optional[HealthSnapshot] = None

        # Callbacks
        self._anomaly_callbacks: List[Callable[[HealthSnapshot, AnomalyType, Dict[str, Any]], Coroutine[Any, Any, None]]] = []

    # -------------------------------------------------------------------------
    # Collection
    # -------------------------------------------------------------------------

    async def _collect_snapshot(self) -> HealthSnapshot:
        """Collect a health snapshot from all sources."""
        import socket

        now = time.time()

        # Get proxy state
        proxy_state = "UNKNOWN"
        proxy_pid = None
        proxy_uptime = 0.0

        if self._lifecycle:
            proxy_state = self._lifecycle.state.name
            proxy_pid = self._lifecycle.pid
            proxy_uptime = self._lifecycle.uptime_seconds or 0.0

        # Measure TCP latency
        tcp_latency = 0.0
        try:
            start = time.monotonic()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", 5432))
            sock.close()
            tcp_latency = (time.monotonic() - start) * 1000
        except Exception:
            tcp_latency = -1.0  # Indicates failure

        # Get consecutive failures from lifecycle
        consecutive_failures = 0
        if self._lifecycle:
            status = self._lifecycle.get_status()
            consecutive_failures = status.get("consecutive_failures", 0)

        snapshot = HealthSnapshot(
            timestamp=now,
            correlation_id=self._correlation_id,
            source_repo=self._source_repo,
            proxy_state=proxy_state,
            proxy_pid=proxy_pid,
            proxy_uptime_seconds=proxy_uptime,
            tcp_latency_ms=tcp_latency if tcp_latency > 0 else 0.0,
            db_latency_ms=0.0,  # TODO: Add DB query timing
            tls_latency_ms=0.0,  # Handled by proxy
            consecutive_failures=consecutive_failures,
            error_count=1 if tcp_latency < 0 else 0,
        )

        # Run anomaly detection
        is_anomaly, anomaly_type, anomaly_details = self._anomaly_detector.detect(snapshot)

        if is_anomaly and anomaly_type:
            snapshot = HealthSnapshot(
                **{**asdict(snapshot), "is_anomaly": True, "anomaly_type": anomaly_type.name, "anomaly_details": anomaly_details}
            )

            # Fire callbacks
            for callback in self._anomaly_callbacks:
                try:
                    await callback(snapshot, anomaly_type, anomaly_details)
                except Exception as e:
                    logger.error(f"[HealthAggregator] Anomaly callback error: {e}")

        return snapshot

    async def _collection_loop(self) -> None:
        """Background collection loop."""
        while self._running:
            try:
                snapshot = await self._collect_snapshot()
                self._last_snapshot = snapshot

                # Log event
                event = HealthEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=EventType.SNAPSHOT,
                    timestamp=snapshot.timestamp,
                    correlation_id=snapshot.correlation_id,
                    source_repo=snapshot.source_repo,
                    data=snapshot.to_dict(),
                )
                await self._event_store.append(event)

                # Log anomalies separately
                if snapshot.is_anomaly:
                    anomaly_event = HealthEvent(
                        event_id=str(uuid.uuid4()),
                        event_type=EventType.ANOMALY_DETECTED,
                        timestamp=snapshot.timestamp,
                        correlation_id=snapshot.correlation_id,
                        source_repo=snapshot.source_repo,
                        data={
                            "anomaly_type": snapshot.anomaly_type,
                            "anomaly_details": snapshot.anomaly_details,
                        },
                    )
                    await self._event_store.append(anomaly_event)

                    logger.warning(
                        f"[HealthAggregator] Anomaly detected: {snapshot.anomaly_type}"
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HealthAggregator] Collection error: {e}")

            await asyncio.sleep(HealthConfig.COLLECTION_INTERVAL)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Start health collection."""
        if self._running:
            return

        self._running = True
        self._collection_task = asyncio.create_task(self._collection_loop())
        logger.info(f"[HealthAggregator] Started for repo: {self._source_repo}")

    async def stop(self) -> None:
        """Stop health collection."""
        self._running = False

        if self._collection_task:
            self._collection_task.cancel()
            try:
                await self._collection_task
            except asyncio.CancelledError:
                pass

        logger.info("[HealthAggregator] Stopped")

    def add_anomaly_callback(
        self,
        callback: Callable[[HealthSnapshot, AnomalyType, Dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a callback for anomaly events."""
        self._anomaly_callbacks.append(callback)

    async def log_state_change(
        self,
        from_state: str,
        to_state: str,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a state change event."""
        event = HealthEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.STATE_CHANGE,
            timestamp=time.time(),
            correlation_id=self._correlation_id,
            source_repo=self._source_repo,
            data={
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
            },
            metadata=metadata or {},
        )
        await self._event_store.append(event)

    async def log_error(
        self,
        message: str,
        error_type: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an error event."""
        event = HealthEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.ERROR,
            timestamp=time.time(),
            correlation_id=self._correlation_id,
            source_repo=self._source_repo,
            data={
                "message": message,
                "error_type": error_type,
            },
            metadata=metadata or {},
        )
        await self._event_store.append(event)

    async def diagnose(
        self,
        failure_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run diagnosis for a failure (defaults to most recent)."""
        if failure_time is None:
            failure_time = time.time()

        return await self._diagnosis_engine.diagnose_failure(
            failure_time,
            self._correlation_id,
        )

    def get_current_snapshot(self) -> Optional[HealthSnapshot]:
        """Get the most recent health snapshot."""
        return self._last_snapshot

    def get_latency_stats(self) -> Dict[str, float]:
        """Get current latency statistics."""
        return self._anomaly_detector.get_latency_stats()

    def get_recent_anomalies(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent anomalies."""
        return self._anomaly_detector.get_recent_anomalies(limit)

    async def query_events(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        event_types: Optional[Set[EventType]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query health events."""
        events = await self._event_store.query(
            start_time=start_time,
            end_time=end_time,
            event_types=event_types,
            limit=limit,
        )
        return [
            {
                "event_id": e.event_id,
                "event_type": e.event_type.value,
                "timestamp": e.timestamp,
                "correlation_id": e.correlation_id,
                "data": e.data,
            }
            for e in events
        ]

    def get_status(self) -> Dict[str, Any]:
        """Get current aggregator status."""
        return {
            "running": self._running,
            "source_repo": self._source_repo,
            "correlation_id": self._correlation_id,
            "last_snapshot": self._last_snapshot.to_dict() if self._last_snapshot else None,
            "latency_stats": self.get_latency_stats(),
            "recent_anomalies": self.get_recent_anomalies(5),
        }


# =============================================================================
# Factory Function
# =============================================================================

async def create_health_aggregator(
    source_repo: str = "jarvis",
    lifecycle_controller: Optional[ProxyLifecycleController] = None,
    auto_start: bool = True,
) -> UnifiedHealthAggregator:
    """
    Factory function to create a health aggregator.

    Args:
        source_repo: Name of the source repository
        lifecycle_controller: Optional proxy lifecycle controller
        auto_start: Whether to start collection automatically

    Returns:
        Configured UnifiedHealthAggregator
    """
    aggregator = UnifiedHealthAggregator(
        source_repo=source_repo,
        lifecycle_controller=lifecycle_controller,
    )

    if auto_start:
        await aggregator.start()

    return aggregator
