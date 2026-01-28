"""
Docker Daemon Manager v11.0 - Intelligent Self-Healing Engine

Enterprise-grade async Docker daemon management with:
- Multi-signal diagnostic engine (15+ health signals)
- 4-level progressive self-healing recovery
- Adaptive circuit breaker with exponential cooldown
- Predictive startup with ML-based timeout calculation
- Historical learning database (SQLite-backed)
- Cross-repo coordination via Trinity Protocol
- Zero hardcoding - fully configurable via environment variables

ROOT CAUSE FIXES (v11.0):
- Fixed macOS process detection (com.docker.backend, not 'Docker Desktop')
- Added comprehensive socket discovery with symlink resolution
- Added resource pre-flight checks before startup attempts
- Added intelligent failure pattern recognition

Author: JARVIS AGI System
Version: 11.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import wraps
from pathlib import Path
from typing import (
    Any, AsyncGenerator, AsyncIterator, Callable, Coroutine,
    Dict, Final, List, Optional, Set, Tuple, TypeVar, Union
)

import psutil

# Type variable for generic decorators
T = TypeVar('T')
F = TypeVar('F', bound=Callable[..., Any])

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS & CONSTANTS
# =============================================================================

class DaemonStatus(Enum):
    """Docker daemon status states with semantic meaning"""
    UNKNOWN = "unknown"
    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_RUNNING = "installed_not_running"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"  # Running but not fully healthy
    STOPPING = "stopping"
    ERROR = "error"
    RECOVERING = "recovering"


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, rejecting requests
    HALF_OPEN = auto()   # Testing recovery


class RecoveryLevel(Enum):
    """Progressive recovery levels"""
    LEVEL_1_SOFT = 1       # Minimal intervention
    LEVEL_2_RESET = 2      # Restart Docker Desktop
    LEVEL_3_DEEP = 3       # Reset state files
    LEVEL_4_NUCLEAR = 4    # Factory reset (requires confirmation)


class DockerStateEvent(Enum):
    """Docker state events for cross-repo coordination"""
    # Lifecycle events
    STARTING = "docker.starting"
    STARTED = "docker.started"
    STOPPING = "docker.stopping"
    STOPPED = "docker.stopped"

    # Health events
    HEALTHY = "docker.healthy"
    UNHEALTHY = "docker.unhealthy"
    DEGRADED = "docker.degraded"

    # Recovery events
    RECOVERY_STARTED = "docker.recovery.started"
    RECOVERY_LEVEL_ESCALATED = "docker.recovery.escalated"
    RECOVERY_SUCCEEDED = "docker.recovery.succeeded"
    RECOVERY_FAILED = "docker.recovery.failed"

    # Resource events
    RESOURCE_WARNING = "docker.resource.warning"
    RESOURCE_CRITICAL = "docker.resource.critical"


# Platform-specific Docker process names (ROOT CAUSE FIX)
# These are the ACTUAL process names, not app names
MACOS_DOCKER_PROCESSES: Final[Dict[str, int]] = {
    # Process name -> priority (lower = more important indicator)
    "com.docker.backend": 1,          # Main backend (most reliable)
    "Docker": 2,                       # The app process
    "com.docker.hyperkit": 3,          # Legacy Intel VM runtime
    "com.docker.virtualization": 3,    # Apple Silicon VM runtime
    "docker": 4,                       # CLI (less useful)
    "containerd": 5,                   # Container runtime
    "com.docker.vmnetd": 10,           # Network helper (always running - least useful)
}

LINUX_DOCKER_PROCESSES: Final[Dict[str, int]] = {
    "dockerd": 1,                      # Main daemon
    "containerd": 2,                   # Container runtime
    "docker-proxy": 3,                 # Network proxy
    "containerd-shim": 4,              # Container shims
}

WINDOWS_DOCKER_PROCESSES: Final[Dict[str, int]] = {
    "Docker Desktop.exe": 1,           # Main app
    "com.docker.backend.exe": 2,       # Backend
    "com.docker.proxy.exe": 3,         # Proxy
    "dockerd.exe": 4,                  # Daemon
}


# =============================================================================
# CONFIGURATION (Zero Hardcoding - All from Environment)
# =============================================================================

@dataclass
class DockerConfig:
    """
    Dynamic Docker configuration - ZERO HARDCODING

    All values configurable via environment variables with sensible defaults.
    """

    # === Startup Settings ===
    max_startup_wait_seconds: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_MAX_STARTUP_WAIT', '180'))
    )
    min_startup_wait_seconds: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_MIN_STARTUP_WAIT', '30'))
    )
    poll_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_POLL_INTERVAL', '2.0'))
    )
    max_retry_attempts: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_MAX_RETRIES', '3'))
    )

    # === Health Check Settings ===
    enable_parallel_health_checks: bool = field(
        default_factory=lambda: os.getenv('DOCKER_PARALLEL_HEALTH', 'true').lower() == 'true'
    )
    health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_HEALTH_TIMEOUT', '10.0'))
    )
    process_check_timeout: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_PROCESS_CHECK_TIMEOUT', '3.0'))
    )

    # === Application Paths (Platform-specific defaults) ===
    docker_app_path_macos: str = field(
        default_factory=lambda: os.getenv('DOCKER_APP_MACOS', '/Applications/Docker.app')
    )
    docker_app_path_windows: str = field(
        default_factory=lambda: os.getenv('DOCKER_APP_WINDOWS', 'Docker Desktop')
    )

    # === Retry Settings ===
    retry_backoff_base: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_RETRY_BACKOFF', '1.5'))
    )
    retry_backoff_max: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_RETRY_BACKOFF_MAX', '15.0'))
    )

    # === Circuit Breaker Settings ===
    cb_failure_threshold: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_CB_FAILURE_THRESHOLD', '3'))
    )
    cb_initial_cooldown_seconds: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_CB_INITIAL_COOLDOWN', '30.0'))
    )
    cb_max_cooldown_seconds: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_CB_MAX_COOLDOWN', '300.0'))
    )
    cb_cooldown_multiplier: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_CB_COOLDOWN_MULTIPLIER', '2.0'))
    )
    cb_half_open_max_requests: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_CB_HALF_OPEN_REQUESTS', '1'))
    )

    # === Resource Thresholds ===
    min_memory_gb: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_MIN_MEMORY_GB', '2.0'))
    )
    min_disk_gb: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_MIN_DISK_GB', '10.0'))
    )
    max_cpu_percent: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_MAX_CPU_PERCENT', '90.0'))
    )

    # === Learning Database ===
    learning_db_path: str = field(
        default_factory=lambda: os.getenv(
            'DOCKER_LEARNING_DB',
            str(Path.home() / '.jarvis' / 'docker_learning.db')
        )
    )
    enable_learning: bool = field(
        default_factory=lambda: os.getenv('DOCKER_ENABLE_LEARNING', 'true').lower() == 'true'
    )

    # === Cross-Repo Coordination ===
    enable_cross_repo: bool = field(
        default_factory=lambda: os.getenv('DOCKER_CROSS_REPO_ENABLED', 'true').lower() == 'true'
    )
    trinity_state_dir: str = field(
        default_factory=lambda: os.getenv(
            'TRINITY_STATE_DIR',
            str(Path.home() / '.jarvis' / 'trinity')
        )
    )

    # === Self-Healing Settings ===
    enable_self_healing: bool = field(
        default_factory=lambda: os.getenv('DOCKER_SELF_HEALING', 'true').lower() == 'true'
    )
    max_healing_level: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_MAX_HEALING_LEVEL', '3'))
    )
    require_confirmation_level_4: bool = field(
        default_factory=lambda: os.getenv('DOCKER_REQUIRE_CONFIRM_L4', 'true').lower() == 'true'
    )

    # === Diagnostics ===
    enable_verbose_logging: bool = field(
        default_factory=lambda: os.getenv('DOCKER_VERBOSE', 'false').lower() == 'true'
    )
    diagnostic_snapshot_enabled: bool = field(
        default_factory=lambda: os.getenv('DOCKER_DIAGNOSTIC_SNAPSHOT', 'true').lower() == 'true'
    )

    def __post_init__(self):
        """Validate configuration"""
        if self.min_startup_wait_seconds > self.max_startup_wait_seconds:
            self.min_startup_wait_seconds = self.max_startup_wait_seconds // 2

        if self.cb_initial_cooldown_seconds > self.cb_max_cooldown_seconds:
            self.cb_initial_cooldown_seconds = self.cb_max_cooldown_seconds / 10


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ProcessSignal:
    """Result of a single process check"""
    name: str
    running: bool
    pid: Optional[int] = None
    cpu_percent: Optional[float] = None
    memory_mb: Optional[float] = None
    priority: int = 10  # Lower = more important


@dataclass
class SocketSignal:
    """Result of a socket check"""
    path: str
    exists: bool
    is_symlink: bool = False
    resolved_path: Optional[str] = None
    readable: bool = False
    writable: bool = False
    age_seconds: Optional[float] = None
    responsive: bool = False


@dataclass
class ResourceSignal:
    """System resource availability"""
    memory_available_gb: float
    memory_total_gb: float
    memory_percent_used: float
    disk_available_gb: float
    disk_total_gb: float
    disk_percent_used: float
    cpu_percent: float
    memory_pressure: str  # 'normal', 'warning', 'critical'
    sufficient_for_docker: bool


@dataclass
class StateSignal:
    """Docker state file information"""
    settings_exist: bool
    vm_disk_exists: bool
    vm_disk_size_gb: Optional[float] = None
    lock_files: List[str] = field(default_factory=list)
    crash_reports: List[str] = field(default_factory=list)
    update_in_progress: bool = False
    last_successful_start: Optional[float] = None


@dataclass
class FailurePattern:
    """Recognized failure pattern with solution"""
    pattern_id: str
    symptoms: List[str]
    root_cause: str
    solution_strategy: RecoveryLevel
    success_rate: float  # 0.0-1.0 from historical data
    avg_recovery_time_ms: int

    def matches(self, signals: Dict[str, Any]) -> Tuple[bool, float]:
        """Check if this pattern matches current signals"""
        matched_symptoms = 0
        for symptom in self.symptoms:
            if self._check_symptom(symptom, signals):
                matched_symptoms += 1

        if matched_symptoms == 0:
            return False, 0.0

        confidence = matched_symptoms / len(self.symptoms)
        return confidence >= 0.5, confidence

    def _check_symptom(self, symptom: str, signals: Dict[str, Any]) -> bool:
        """Check a single symptom against signals"""
        # Parse symptom format: "signal_type.field.condition"
        parts = symptom.split('.')

        try:
            value = signals
            for part in parts[:-1]:
                value = value.get(part, {})

            condition = parts[-1]

            if condition == 'exists':
                return bool(value)
            elif condition == 'not_exists':
                return not bool(value)
            elif condition == 'true':
                return value is True
            elif condition == 'false':
                return value is False
            elif condition.startswith('gt_'):
                threshold = float(condition[3:])
                if isinstance(value, (int, float, str)):
                    return float(value) > threshold
                return False
            elif condition.startswith('lt_'):
                threshold = float(condition[3:])
                if isinstance(value, (int, float, str)):
                    return float(value) < threshold
                return False
            else:
                return value == condition
        except (KeyError, TypeError, ValueError):
            return False


# Known failure patterns (loaded from DB + these defaults)
DEFAULT_FAILURE_PATTERNS: List[FailurePattern] = [
    FailurePattern(
        pattern_id="stale_socket",
        symptoms=[
            "sockets.primary.exists.true",
            "sockets.primary.responsive.false",
            "processes.backend.running.false"
        ],
        root_cause="Stale socket from previously crashed Docker",
        solution_strategy=RecoveryLevel.LEVEL_1_SOFT,
        success_rate=0.95,
        avg_recovery_time_ms=5000,
    ),
    FailurePattern(
        pattern_id="vm_frozen",
        symptoms=[
            "processes.backend.running.true",
            "sockets.primary.exists.true",
            "sockets.primary.responsive.false"
        ],
        root_cause="Docker VM is frozen or unresponsive",
        solution_strategy=RecoveryLevel.LEVEL_2_RESET,
        success_rate=0.88,
        avg_recovery_time_ms=30000,
    ),
    FailurePattern(
        pattern_id="resource_exhaustion",
        symptoms=[
            "resources.memory_pressure.critical",
            "resources.sufficient_for_docker.false"
        ],
        root_cause="Insufficient system resources (memory/disk)",
        solution_strategy=RecoveryLevel.LEVEL_1_SOFT,
        success_rate=0.72,
        avg_recovery_time_ms=15000,
    ),
    FailurePattern(
        pattern_id="update_stuck",
        symptoms=[
            "state.update_in_progress.true",
            "state.lock_files.exists"
        ],
        root_cause="Docker Desktop update was interrupted",
        solution_strategy=RecoveryLevel.LEVEL_3_DEEP,
        success_rate=0.65,
        avg_recovery_time_ms=60000,
    ),
    FailurePattern(
        pattern_id="clean_not_running",
        symptoms=[
            "processes.backend.running.false",
            "sockets.primary.exists.false",
            "resources.sufficient_for_docker.true"
        ],
        root_cause="Docker Desktop simply not started",
        solution_strategy=RecoveryLevel.LEVEL_1_SOFT,
        success_rate=0.98,
        avg_recovery_time_ms=20000,
    ),
    FailurePattern(
        pattern_id="partial_startup",
        symptoms=[
            "processes.app.running.true",
            "processes.backend.running.false",
            "sockets.primary.responsive.false"
        ],
        root_cause="Docker Desktop started but backend failed to initialize",
        solution_strategy=RecoveryLevel.LEVEL_2_RESET,
        success_rate=0.82,
        avg_recovery_time_ms=25000,
    ),
    FailurePattern(
        pattern_id="corrupted_state",
        symptoms=[
            "state.crash_reports.exists",
            "processes.backend.running.false",
            "state.vm_disk_exists.false"
        ],
        root_cause="Docker state files are corrupted",
        solution_strategy=RecoveryLevel.LEVEL_3_DEEP,
        success_rate=0.70,
        avg_recovery_time_ms=90000,
    ),
]


@dataclass
class DiagnosticResult:
    """Comprehensive diagnostic analysis result"""

    # Overall assessment
    health_score: float  # 0.0 (dead) to 1.0 (perfect)
    status: DaemonStatus
    can_auto_heal: bool

    # Signal results
    process_signals: Dict[str, ProcessSignal]
    socket_signals: Dict[str, SocketSignal]
    resource_signals: ResourceSignal
    state_signals: StateSignal

    # Pattern matching
    matched_patterns: List[FailurePattern]
    confidence_scores: Dict[str, float]

    # Recommended actions
    recommended_strategy: RecoveryLevel
    estimated_recovery_ms: int
    blockers: List[str]

    # Timing
    diagnosis_time_ms: int
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'health_score': self.health_score,
            'status': self.status.value,
            'can_auto_heal': self.can_auto_heal,
            'process_signals': {
                k: {'running': v.running, 'pid': v.pid}
                for k, v in self.process_signals.items()
            },
            'socket_signals': {
                k: {'exists': v.exists, 'responsive': v.responsive}
                for k, v in self.socket_signals.items()
            },
            'resource_signals': {
                'memory_available_gb': self.resource_signals.memory_available_gb,
                'disk_available_gb': self.resource_signals.disk_available_gb,
                'memory_pressure': self.resource_signals.memory_pressure,
                'sufficient': self.resource_signals.sufficient_for_docker,
            },
            'matched_patterns': [p.pattern_id for p in self.matched_patterns],
            'recommended_strategy': self.recommended_strategy.name,
            'estimated_recovery_ms': self.estimated_recovery_ms,
            'blockers': self.blockers,
            'diagnosis_time_ms': self.diagnosis_time_ms,
            'timestamp': self.timestamp,
        }


@dataclass
class DaemonHealth:
    """Docker daemon health metrics (backward compatible)"""
    status: DaemonStatus
    daemon_responsive: bool = False
    api_accessible: bool = False
    containers_queryable: bool = False
    socket_exists: bool = False
    process_running: bool = False
    startup_time_ms: int = 0
    error_message: Optional[str] = None
    last_check_timestamp: float = field(default_factory=time.time)
    diagnostic_result: Optional[DiagnosticResult] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'status': self.status.value,
            'daemon_responsive': self.daemon_responsive,
            'api_accessible': self.api_accessible,
            'containers_queryable': self.containers_queryable,
            'socket_exists': self.socket_exists,
            'process_running': self.process_running,
            'startup_time_ms': self.startup_time_ms,
            'error': self.error_message,
            'last_check': self.last_check_timestamp,
        }

    def is_healthy(self) -> bool:
        """Check if daemon is fully healthy"""
        return (
            self.status == DaemonStatus.RUNNING and
            self.daemon_responsive and
            self.api_accessible
        )


@dataclass
class RecoveryResult:
    """Result of a recovery attempt"""
    success: bool
    level: RecoveryLevel
    actions_taken: List[str]
    duration_ms: int
    error: Optional[str] = None
    requires_confirmation: bool = False
    next_recommended_level: Optional[RecoveryLevel] = None


@dataclass
class StartupPrediction:
    """ML-based startup prediction"""
    predicted_success_probability: float
    predicted_startup_time_ms: int
    recommended_strategy: RecoveryLevel
    confidence_interval: Tuple[float, float]
    factor_weights: Dict[str, float]
    risk_factors: List[str]
    optimization_suggestions: List[str]


@dataclass
class TimeoutConfig:
    """Adaptive timeout configuration"""
    total_timeout_seconds: float
    poll_interval_seconds: float
    progress_update_interval_seconds: float
    factors: Dict[str, float]


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class AdaptiveCircuitBreaker:
    """
    Circuit breaker with adaptive cooldown based on failure patterns.

    Prevents hammering a failing Docker daemon while allowing quick
    recovery when conditions improve.
    """

    def __init__(self, config: DockerConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.last_success_time: Optional[float] = None
        self.cooldown_seconds = config.cb_initial_cooldown_seconds
        self.half_open_requests = 0
        self._lock = asyncio.Lock()

    async def should_attempt(self) -> Tuple[bool, str]:
        """
        Check if we should attempt a Docker operation.

        Returns:
            (should_attempt, reason)
        """
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True, "Circuit closed - normal operation"

            elif self.state == CircuitState.OPEN:
                if self._cooldown_elapsed():
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_requests = 0
                    logger.info("Circuit breaker transitioning to HALF_OPEN")
                    return True, "Circuit half-open - testing recovery"
                else:
                    remaining = self._cooldown_remaining()
                    return False, f"Circuit open - wait {remaining:.0f}s (cooldown)"

            elif self.state == CircuitState.HALF_OPEN:
                if self.half_open_requests < self.config.cb_half_open_max_requests:
                    self.half_open_requests += 1
                    return True, "Circuit half-open - test in progress"
                else:
                    return False, "Circuit half-open - waiting for test result"

            return False, "Unknown circuit state"

    async def record_success(self):
        """Record a successful operation"""
        async with self._lock:
            self.success_count += 1
            self.last_success_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Recovery successful - close circuit
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.cooldown_seconds = self.config.cb_initial_cooldown_seconds
                logger.info("Circuit breaker CLOSED - recovery successful")

            elif self.state == CircuitState.CLOSED:
                # Reset failure count on success
                self.failure_count = max(0, self.failure_count - 1)

    async def record_failure(self, error: Optional[str] = None):
        """Record a failed operation"""
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Test failed - reopen with extended cooldown
                self.state = CircuitState.OPEN
                self.cooldown_seconds = min(
                    self.cooldown_seconds * self.config.cb_cooldown_multiplier,
                    self.config.cb_max_cooldown_seconds
                )
                logger.warning(
                    f"Circuit breaker OPEN - test failed, cooldown extended to {self.cooldown_seconds:.0f}s"
                )

            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.config.cb_failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning(
                        f"Circuit breaker OPEN - {self.failure_count} failures, "
                        f"cooldown {self.cooldown_seconds:.0f}s"
                    )

    def _cooldown_elapsed(self) -> bool:
        """Check if cooldown period has elapsed"""
        if self.last_failure_time is None:
            return True
        return (time.time() - self.last_failure_time) >= self.cooldown_seconds

    def _cooldown_remaining(self) -> float:
        """Get remaining cooldown time"""
        if self.last_failure_time is None:
            return 0
        elapsed = time.time() - self.last_failure_time
        return max(0, self.cooldown_seconds - elapsed)

    def get_state(self) -> Dict[str, Any]:
        """Get current circuit breaker state"""
        return {
            'state': self.state.name,
            'failure_count': self.failure_count,
            'success_count': self.success_count,
            'cooldown_seconds': self.cooldown_seconds,
            'cooldown_remaining': self._cooldown_remaining() if self.state == CircuitState.OPEN else 0,
        }


# =============================================================================
# LEARNING DATABASE
# =============================================================================

class DockerLearningDB:
    """
    SQLite-backed learning database for Docker startup patterns.

    Tracks historical startup attempts to improve predictions
    and recovery strategies over time.
    """

    SCHEMA = """
    -- Startup attempts with full context
    CREATE TABLE IF NOT EXISTS startup_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        machine_id TEXT NOT NULL,

        -- Pre-startup state
        initial_health_score REAL,
        memory_available_gb REAL,
        disk_available_gb REAL,
        cpu_load_percent REAL,
        memory_pressure TEXT,

        -- Diagnostic results
        matched_pattern_id TEXT,
        diagnostic_signals TEXT,

        -- Recovery action
        recovery_level INTEGER,
        recovery_strategy TEXT,

        -- Outcome
        success INTEGER NOT NULL,
        startup_time_ms INTEGER,
        error_message TEXT,

        -- Learning metadata
        prediction_probability REAL,
        actual_outcome REAL
    );

    -- Pattern effectiveness tracking
    CREATE TABLE IF NOT EXISTS pattern_effectiveness (
        pattern_id TEXT PRIMARY KEY,
        total_matches INTEGER DEFAULT 0,
        successful_recoveries INTEGER DEFAULT 0,
        avg_recovery_time_ms REAL,
        last_success_timestamp REAL,
        last_failure_timestamp REAL,
        effectiveness_score REAL
    );

    -- Temporal patterns (hour of day)
    CREATE TABLE IF NOT EXISTS temporal_patterns (
        hour_of_day INTEGER PRIMARY KEY,
        total_attempts INTEGER DEFAULT 0,
        successful_attempts INTEGER DEFAULT 0,
        avg_startup_time_ms REAL,
        avg_memory_available_gb REAL
    );

    -- Feature weights for prediction model
    CREATE TABLE IF NOT EXISTS feature_weights (
        feature_name TEXT PRIMARY KEY,
        weight REAL DEFAULT 0.0,
        last_updated REAL
    );

    -- Indexes
    CREATE INDEX IF NOT EXISTS idx_startup_timestamp ON startup_attempts(timestamp);
    CREATE INDEX IF NOT EXISTS idx_startup_success ON startup_attempts(success);
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._machine_id = self._get_machine_id()

    def _get_machine_id(self) -> str:
        """Get unique machine identifier"""
        try:
            # Use hostname + username for uniqueness
            unique_str = f"{platform.node()}:{os.getlogin()}"
            return hashlib.sha256(unique_str.encode()).hexdigest()[:16]
        except Exception:
            return str(uuid.uuid4())[:16]

    async def initialize(self):
        """Initialize database and create tables"""
        async with self._lock:
            if self._connection is None:
                self._connection = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False
                )
                self._connection.row_factory = sqlite3.Row
                self._connection.executescript(self.SCHEMA)
                self._connection.commit()
                logger.info(f"Docker learning database initialized: {self.db_path}")

    async def close(self):
        """Close database connection"""
        async with self._lock:
            if self._connection:
                self._connection.close()
                self._connection = None

    async def record_attempt(
        self,
        diagnostic: DiagnosticResult,
        recovery_level: RecoveryLevel,
        success: bool,
        startup_time_ms: int,
        error_message: Optional[str] = None,
        prediction_probability: Optional[float] = None
    ):
        """Record a startup attempt for learning"""
        async with self._lock:
            if not self._connection:
                return

            try:
                self._connection.execute(
                    """
                    INSERT INTO startup_attempts (
                        timestamp, machine_id, initial_health_score,
                        memory_available_gb, disk_available_gb, cpu_load_percent,
                        memory_pressure, matched_pattern_id, diagnostic_signals,
                        recovery_level, recovery_strategy, success, startup_time_ms,
                        error_message, prediction_probability, actual_outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        self._machine_id,
                        diagnostic.health_score,
                        diagnostic.resource_signals.memory_available_gb,
                        diagnostic.resource_signals.disk_available_gb,
                        diagnostic.resource_signals.cpu_percent,
                        diagnostic.resource_signals.memory_pressure,
                        diagnostic.matched_patterns[0].pattern_id if diagnostic.matched_patterns else None,
                        json.dumps(diagnostic.to_dict()),
                        recovery_level.value,
                        recovery_level.name,
                        1 if success else 0,
                        startup_time_ms,
                        error_message,
                        prediction_probability,
                        1.0 if success else 0.0
                    )
                )
                self._connection.commit()

                # Update pattern effectiveness
                if diagnostic.matched_patterns:
                    await self._update_pattern_effectiveness(
                        diagnostic.matched_patterns[0].pattern_id,
                        success,
                        startup_time_ms
                    )

                # Update temporal patterns
                await self._update_temporal_pattern(
                    datetime.now().hour,
                    success,
                    startup_time_ms,
                    diagnostic.resource_signals.memory_available_gb
                )

            except Exception as e:
                logger.warning(f"Failed to record startup attempt: {e}")

    async def _update_pattern_effectiveness(
        self,
        pattern_id: str,
        success: bool,
        recovery_time_ms: int
    ):
        """Update pattern effectiveness metrics"""
        if not self._connection:
            return

        try:
            # Get current stats
            cursor = self._connection.execute(
                "SELECT * FROM pattern_effectiveness WHERE pattern_id = ?",
                (pattern_id,)
            )
            row = cursor.fetchone()

            if row:
                total = row['total_matches'] + 1
                successes = row['successful_recoveries'] + (1 if success else 0)
                avg_time = (row['avg_recovery_time_ms'] * row['total_matches'] + recovery_time_ms) / total

                self._connection.execute(
                    """
                    UPDATE pattern_effectiveness SET
                        total_matches = ?,
                        successful_recoveries = ?,
                        avg_recovery_time_ms = ?,
                        last_success_timestamp = CASE WHEN ? THEN ? ELSE last_success_timestamp END,
                        last_failure_timestamp = CASE WHEN ? THEN last_failure_timestamp ELSE ? END,
                        effectiveness_score = ?
                    WHERE pattern_id = ?
                    """,
                    (
                        total, successes, avg_time,
                        success, time.time(),
                        success, time.time(),
                        successes / total if total > 0 else 0,
                        pattern_id
                    )
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO pattern_effectiveness (
                        pattern_id, total_matches, successful_recoveries,
                        avg_recovery_time_ms, last_success_timestamp,
                        last_failure_timestamp, effectiveness_score
                    ) VALUES (?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        pattern_id,
                        1 if success else 0,
                        recovery_time_ms,
                        time.time() if success else None,
                        None if success else time.time(),
                        1.0 if success else 0.0
                    )
                )

            self._connection.commit()
        except Exception as e:
            logger.debug(f"Failed to update pattern effectiveness: {e}")

    async def _update_temporal_pattern(
        self,
        hour: int,
        success: bool,
        startup_time_ms: int,
        memory_gb: float
    ):
        """Update time-of-day patterns"""
        if not self._connection:
            return

        try:
            cursor = self._connection.execute(
                "SELECT * FROM temporal_patterns WHERE hour_of_day = ?",
                (hour,)
            )
            row = cursor.fetchone()

            if row:
                total = row['total_attempts'] + 1
                successes = row['successful_attempts'] + (1 if success else 0)
                avg_time = (row['avg_startup_time_ms'] * row['total_attempts'] + startup_time_ms) / total
                avg_memory = (row['avg_memory_available_gb'] * row['total_attempts'] + memory_gb) / total

                self._connection.execute(
                    """
                    UPDATE temporal_patterns SET
                        total_attempts = ?,
                        successful_attempts = ?,
                        avg_startup_time_ms = ?,
                        avg_memory_available_gb = ?
                    WHERE hour_of_day = ?
                    """,
                    (total, successes, avg_time, avg_memory, hour)
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO temporal_patterns (
                        hour_of_day, total_attempts, successful_attempts,
                        avg_startup_time_ms, avg_memory_available_gb
                    ) VALUES (?, 1, ?, ?, ?)
                    """,
                    (hour, 1 if success else 0, startup_time_ms, memory_gb)
                )

            self._connection.commit()
        except Exception as e:
            logger.debug(f"Failed to update temporal pattern: {e}")

    async def get_hour_statistics(self, hour: int) -> Dict[str, Any]:
        """Get statistics for a specific hour of day"""
        async with self._lock:
            if not self._connection:
                return {'success_rate': 0.7, 'avg_startup_time_ms': 30000}

            try:
                cursor = self._connection.execute(
                    "SELECT * FROM temporal_patterns WHERE hour_of_day = ?",
                    (hour,)
                )
                row = cursor.fetchone()

                if row and row['total_attempts'] > 0:
                    return {
                        'success_rate': row['successful_attempts'] / row['total_attempts'],
                        'avg_startup_time_ms': row['avg_startup_time_ms'],
                        'avg_memory_gb': row['avg_memory_available_gb'],
                        'sample_count': row['total_attempts'],
                    }
                else:
                    return {'success_rate': 0.7, 'avg_startup_time_ms': 30000, 'sample_count': 0}
            except Exception:
                return {'success_rate': 0.7, 'avg_startup_time_ms': 30000, 'sample_count': 0}

    async def get_pattern_effectiveness(self, pattern_id: str) -> Dict[str, Any]:
        """Get effectiveness metrics for a pattern"""
        async with self._lock:
            if not self._connection:
                return {}

            try:
                cursor = self._connection.execute(
                    "SELECT * FROM pattern_effectiveness WHERE pattern_id = ?",
                    (pattern_id,)
                )
                row = cursor.fetchone()

                if row:
                    return dict(row)
                return {}
            except Exception:
                return {}

    async def get_recent_failure_count(self, hours: int = 1) -> int:
        """Get count of recent failures"""
        async with self._lock:
            if not self._connection:
                return 0

            try:
                cutoff = time.time() - (hours * 3600)
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM startup_attempts WHERE timestamp > ? AND success = 0",
                    (cutoff,)
                )
                return cursor.fetchone()[0]
            except Exception:
                return 0

    async def get_historical_startup_times(self, limit: int = 100) -> List[int]:
        """Get recent successful startup times"""
        async with self._lock:
            if not self._connection:
                return []

            try:
                cursor = self._connection.execute(
                    """
                    SELECT startup_time_ms FROM startup_attempts
                    WHERE success = 1 AND startup_time_ms > 0
                    ORDER BY timestamp DESC LIMIT ?
                    """,
                    (limit,)
                )
                return [row[0] for row in cursor.fetchall()]
            except Exception:
                return []


# =============================================================================
# PREDICTIVE ENGINE
# =============================================================================

class PredictiveStartupEngine:
    """
    ML-based engine for predicting Docker startup outcomes.

    Uses lightweight online learning (no heavy ML dependencies).
    """

    def __init__(self, learning_db: DockerLearningDB, config: DockerConfig):
        self.db = learning_db
        self.config = config
        self._feature_weights: Dict[str, float] = {
            'memory_available': 0.15,
            'disk_available': 0.10,
            'cpu_load': -0.05,
            'memory_pressure': -0.20,
            'recent_failures': -0.15,
            'pattern_match': 0.10,
            'hour_baseline': 0.25,
        }

    async def predict_startup(
        self,
        diagnostic: DiagnosticResult
    ) -> StartupPrediction:
        """Predict startup success probability and optimal strategy"""

        # Get historical baseline for this hour
        hour_stats = await self.db.get_hour_statistics(datetime.now().hour)
        base_probability = hour_stats.get('success_rate', 0.7)

        # Calculate adjustments
        adjustments: List[Tuple[str, float]] = []
        risk_factors: List[str] = []
        suggestions: List[str] = []

        # Memory impact
        memory_gb = diagnostic.resource_signals.memory_available_gb
        if memory_gb < self.config.min_memory_gb:
            penalty = (self.config.min_memory_gb - memory_gb) * 0.15
            adjustments.append(('low_memory', -penalty))
            risk_factors.append(f"Low memory: {memory_gb:.1f}GB available")
            suggestions.append("Close memory-intensive applications before starting Docker")
        elif memory_gb > 8.0:
            adjustments.append(('high_memory', 0.05))

        # Disk impact
        disk_gb = diagnostic.resource_signals.disk_available_gb
        if disk_gb < self.config.min_disk_gb:
            penalty = (self.config.min_disk_gb - disk_gb) * 0.03
            adjustments.append(('low_disk', -penalty))
            risk_factors.append(f"Low disk space: {disk_gb:.1f}GB available")
            suggestions.append("Free up disk space (docker system prune)")

        # Memory pressure impact
        pressure = diagnostic.resource_signals.memory_pressure
        if pressure == 'critical':
            adjustments.append(('memory_pressure_critical', -0.30))
            risk_factors.append("Critical memory pressure detected")
            suggestions.append("System is under heavy memory pressure - consider restarting")
        elif pressure == 'warning':
            adjustments.append(('memory_pressure_warning', -0.15))
            risk_factors.append("Elevated memory pressure")

        # CPU impact
        cpu = diagnostic.resource_signals.cpu_percent
        if cpu > self.config.max_cpu_percent:
            adjustments.append(('high_cpu', -0.10))
            risk_factors.append(f"High CPU usage: {cpu:.0f}%")

        # Pattern match impact
        if diagnostic.matched_patterns:
            best_pattern = max(
                diagnostic.matched_patterns,
                key=lambda p: diagnostic.confidence_scores.get(p.pattern_id, 0)
            )
            pattern_effectiveness = await self.db.get_pattern_effectiveness(best_pattern.pattern_id)
            if pattern_effectiveness:
                eff_score = pattern_effectiveness.get('effectiveness_score', best_pattern.success_rate)
            else:
                eff_score = best_pattern.success_rate

            adjustment = (eff_score - 0.5) * 0.3
            adjustments.append(('pattern_match', adjustment))

        # Recent failure penalty
        recent_failures = await self.db.get_recent_failure_count(hours=1)
        if recent_failures > 0:
            penalty = min(recent_failures * 0.12, 0.40)
            adjustments.append(('recent_failures', -penalty))
            if recent_failures >= 2:
                risk_factors.append(f"{recent_failures} recent failures in last hour")
                suggestions.append("Consider waiting before retrying or escalating recovery level")

        # Calculate final probability
        final_probability = base_probability
        factor_weights = {}

        for factor, adjustment in adjustments:
            final_probability += adjustment
            factor_weights[factor] = adjustment

        # Clamp to valid range
        final_probability = max(0.05, min(0.98, final_probability))

        # Determine recommended strategy based on probability
        if final_probability > 0.85:
            recommended = RecoveryLevel.LEVEL_1_SOFT
        elif final_probability > 0.60:
            recommended = RecoveryLevel.LEVEL_2_RESET
        elif final_probability > 0.35:
            recommended = RecoveryLevel.LEVEL_3_DEEP
        else:
            recommended = RecoveryLevel.LEVEL_3_DEEP  # Skip ahead

        # Override with pattern recommendation if we have high confidence match
        if diagnostic.matched_patterns and diagnostic.confidence_scores:
            best_confidence = max(diagnostic.confidence_scores.values())
            if best_confidence > 0.8:
                best_pattern = [
                    p for p in diagnostic.matched_patterns
                    if diagnostic.confidence_scores.get(p.pattern_id, 0) == best_confidence
                ][0]
                recommended = best_pattern.solution_strategy

        # Predict startup time
        historical_times = await self.db.get_historical_startup_times(50)
        if historical_times:
            avg_time = sum(historical_times) / len(historical_times)
            # Adjust for current conditions
            time_factor = 1.0
            if memory_gb < 4:
                time_factor *= 1.3
            if pressure in ('warning', 'critical'):
                time_factor *= 1.4
            if recommended.value > 1:
                time_factor *= 1.0 + (recommended.value * 0.2)

            predicted_time = int(avg_time * time_factor)
        else:
            predicted_time = 30000  # Default 30s

        # Compute confidence interval
        sample_count = hour_stats.get('sample_count', 0)
        margin = 0.15 if sample_count < 10 else 0.10 if sample_count < 50 else 0.05
        confidence_interval = (
            max(0.0, final_probability - margin),
            min(1.0, final_probability + margin)
        )

        return StartupPrediction(
            predicted_success_probability=final_probability,
            predicted_startup_time_ms=predicted_time,
            recommended_strategy=recommended,
            confidence_interval=confidence_interval,
            factor_weights=factor_weights,
            risk_factors=risk_factors,
            optimization_suggestions=suggestions,
        )


# =============================================================================
# ADAPTIVE TIMEOUT CALCULATOR
# =============================================================================

class AdaptiveTimeoutCalculator:
    """
    Calculates optimal timeouts based on historical data and current conditions.
    """

    def __init__(self, learning_db: DockerLearningDB, config: DockerConfig):
        self.db = learning_db
        self.config = config

    async def calculate_optimal_timeout(
        self,
        diagnostic: DiagnosticResult,
        recovery_level: RecoveryLevel
    ) -> TimeoutConfig:
        """Calculate adaptive timeout for current conditions"""

        # Get historical startup times
        historical_times = await self.db.get_historical_startup_times(100)

        if historical_times:
            # Use percentile-based calculation
            sorted_times = sorted(historical_times)
            p50_idx = len(sorted_times) // 2
            p95_idx = int(len(sorted_times) * 0.95)

            historical_avg = sum(historical_times) / len(historical_times)
            p95_time = sorted_times[min(p95_idx, len(sorted_times) - 1)]

            base_timeout = max(p95_time / 1000, historical_avg / 1000 * 1.5)
        else:
            # No history - use conservative default
            base_timeout = 60.0
            historical_avg = 30000
            p95_time = 60000

        # Resource-adjusted factor
        resource_factor = 1.0

        memory_gb = diagnostic.resource_signals.memory_available_gb
        if memory_gb < self.config.min_memory_gb:
            resource_factor *= 1.5
        elif memory_gb < 4.0:
            resource_factor *= 1.2

        disk_gb = diagnostic.resource_signals.disk_available_gb
        if disk_gb < self.config.min_disk_gb:
            resource_factor *= 1.3

        if diagnostic.resource_signals.memory_pressure == 'critical':
            resource_factor *= 1.5
        elif diagnostic.resource_signals.memory_pressure == 'warning':
            resource_factor *= 1.2

        # Recovery level factor
        level_factors = {
            RecoveryLevel.LEVEL_1_SOFT: 1.0,
            RecoveryLevel.LEVEL_2_RESET: 1.5,
            RecoveryLevel.LEVEL_3_DEEP: 2.0,
            RecoveryLevel.LEVEL_4_NUCLEAR: 3.0,
        }
        level_factor = level_factors.get(recovery_level, 1.5)

        # Cold start detection
        recent_failures = await self.db.get_recent_failure_count(hours=24)
        cold_start_factor = 1.3 if recent_failures == 0 else 1.0

        # Calculate final timeout
        final_timeout = base_timeout * resource_factor * level_factor * cold_start_factor

        # Apply bounds from config
        final_timeout = max(
            self.config.min_startup_wait_seconds,
            min(self.config.max_startup_wait_seconds, final_timeout)
        )

        # Calculate poll interval (more frequent for shorter timeouts)
        poll_interval = max(1.0, min(5.0, final_timeout / 30))

        return TimeoutConfig(
            total_timeout_seconds=final_timeout,
            poll_interval_seconds=poll_interval,
            progress_update_interval_seconds=5.0,
            factors={
                'historical_avg_ms': historical_avg,
                'p95_time_ms': p95_time,
                'resource_factor': resource_factor,
                'level_factor': level_factor,
                'cold_start_factor': cold_start_factor,
                'base_timeout': base_timeout,
            }
        )


# =============================================================================
# CROSS-REPO EVENT EMITTER
# =============================================================================

class DockerEventEmitter:
    """
    Emits Docker state events for cross-repo coordination.

    Uses file-based IPC compatible with Trinity Protocol.
    """

    def __init__(self, config: DockerConfig):
        self.config = config
        self.state_dir = Path(config.trinity_state_dir)
        self.events_file = self.state_dir / "docker_events.json"
        self.state_file = self.state_dir / "docker_state.json"
        self._subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

        # Ensure directories exist
        self.state_dir.mkdir(parents=True, exist_ok=True)

    async def emit(
        self,
        event: DockerStateEvent,
        details: Dict[str, Any],
        health: Optional[DaemonHealth] = None
    ):
        """Emit event to subscribers and persist to file"""

        if not self.config.enable_cross_repo:
            return

        event_data = {
            'event_id': str(uuid.uuid4()),
            'event_type': event.value,
            'timestamp': time.time(),
            'details': details,
            'health': health.to_dict() if health else None,
        }

        # Emit to in-memory subscribers
        for queue in self._subscribers:
            try:
                queue.put_nowait(event_data)
            except asyncio.QueueFull:
                pass

        # Persist to file (atomic write)
        async with self._lock:
            try:
                # Read existing events
                events = []
                if self.events_file.exists():
                    try:
                        events = json.loads(self.events_file.read_text())
                        if not isinstance(events, list):
                            events = []
                    except (json.JSONDecodeError, IOError):
                        events = []

                # Append new event (keep last 100)
                events.append(event_data)
                events = events[-100:]

                # Atomic write
                temp_file = self.events_file.with_suffix('.tmp')
                temp_file.write_text(json.dumps(events, indent=2))
                temp_file.rename(self.events_file)

            except Exception as e:
                logger.debug(f"Failed to persist Docker event: {e}")

    async def update_state(self, health: DaemonHealth, diagnostic: Optional[DiagnosticResult] = None):
        """Update persistent Docker state file"""

        if not self.config.enable_cross_repo:
            return

        state = {
            'timestamp': time.time(),
            'status': health.status.value,
            'healthy': health.is_healthy(),
            'health': health.to_dict(),
            'diagnostic': diagnostic.to_dict() if diagnostic else None,
        }

        async with self._lock:
            try:
                temp_file = self.state_file.with_suffix('.tmp')
                temp_file.write_text(json.dumps(state, indent=2))
                temp_file.rename(self.state_file)
            except Exception as e:
                logger.debug(f"Failed to update Docker state: {e}")

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to Docker events"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """Unsubscribe from Docker events"""
        if queue in self._subscribers:
            self._subscribers.remove(queue)


# =============================================================================
# MAIN DOCKER DAEMON MANAGER v11.0
# =============================================================================

class DockerDaemonManager:
    """
    Production-grade Docker daemon manager with intelligent self-healing.

    Features:
    - Multi-signal diagnostic engine (15+ health signals)
    - 4-level progressive self-healing recovery
    - Adaptive circuit breaker with exponential cooldown
    - Predictive startup with ML-based timeout calculation
    - Historical learning database
    - Cross-repo coordination via Trinity Protocol

    Version: 11.0.0
    """

    VERSION = "11.0.0"

    def __init__(
        self,
        config: Optional[DockerConfig] = None,
        progress_callback: Optional[Callable[[str], None]] = None
    ):
        self.config = config or DockerConfig()
        self.progress_callback = progress_callback
        self.platform = platform.system().lower()

        # State
        self.health = DaemonHealth(status=DaemonStatus.UNKNOWN)
        self._last_diagnostic: Optional[DiagnosticResult] = None

        # Components
        self.circuit_breaker = AdaptiveCircuitBreaker(self.config)
        self.learning_db = DockerLearningDB(self.config.learning_db_path)
        self.predictive_engine: Optional[PredictiveStartupEngine] = None
        self.timeout_calculator: Optional[AdaptiveTimeoutCalculator] = None
        self.event_emitter = DockerEventEmitter(self.config)

        # Failure patterns
        self.failure_patterns = DEFAULT_FAILURE_PATTERNS.copy()

        # Locks
        self._operation_lock = asyncio.Lock()
        self._initialized = False

        logger.info(
            f"Docker Daemon Manager v{self.VERSION} initialized "
            f"(platform: {self.platform}, self-healing: {self.config.enable_self_healing})"
        )

    async def initialize(self):
        """Initialize async components"""
        if self._initialized:
            return

        if self.config.enable_learning:
            await self.learning_db.initialize()
            self.predictive_engine = PredictiveStartupEngine(self.learning_db, self.config)
            self.timeout_calculator = AdaptiveTimeoutCalculator(self.learning_db, self.config)

        self._initialized = True
        logger.info("Docker Daemon Manager async components initialized")

    async def close(self):
        """Cleanup resources"""
        await self.learning_db.close()

    # =========================================================================
    # DIAGNOSTIC ENGINE
    # =========================================================================

    async def diagnose(self) -> DiagnosticResult:
        """
        Run comprehensive diagnostic analysis.

        Checks 15+ signals in parallel to understand Docker's true state.
        """
        start_time = time.time()

        # Run all checks in parallel
        if sys.version_info >= (3, 11):
            async with asyncio.TaskGroup() as tg:
                process_task = tg.create_task(self._check_all_processes())
                socket_task = tg.create_task(self._check_all_sockets())
                resource_task = tg.create_task(self._check_resources())
                state_task = tg.create_task(self._check_state_files())

            process_signals = process_task.result()
            socket_signals = socket_task.result()
            resource_signals = resource_task.result()
            state_signals = state_task.result()
        else:
            results = await asyncio.gather(
                self._check_all_processes(),
                self._check_all_sockets(),
                self._check_resources(),
                self._check_state_files(),
                return_exceptions=True
            )

            process_signals = results[0] if not isinstance(results[0], Exception) else {}
            socket_signals = results[1] if not isinstance(results[1], Exception) else {}
            resource_signals = results[2] if not isinstance(results[2], Exception) else ResourceSignal(
                memory_available_gb=8.0, memory_total_gb=16.0, memory_percent_used=50.0,
                disk_available_gb=50.0, disk_total_gb=500.0, disk_percent_used=90.0,
                cpu_percent=50.0, memory_pressure='normal', sufficient_for_docker=True
            )
            state_signals = results[3] if not isinstance(results[3], Exception) else StateSignal(
                settings_exist=False, vm_disk_exists=False
            )

        # Match failure patterns
        signals_dict = {
            'processes': {k: {'running': v.running, 'pid': v.pid} for k, v in process_signals.items()},
            'sockets': {k: {'exists': v.exists, 'responsive': v.responsive} for k, v in socket_signals.items()},
            'resources': {
                'memory_available_gb': resource_signals.memory_available_gb,
                'disk_available_gb': resource_signals.disk_available_gb,
                'memory_pressure': resource_signals.memory_pressure,
                'sufficient_for_docker': resource_signals.sufficient_for_docker,
            },
            'state': {
                'update_in_progress': state_signals.update_in_progress,
                'lock_files': state_signals.lock_files,
                'crash_reports': state_signals.crash_reports,
                'vm_disk_exists': state_signals.vm_disk_exists,
            }
        }

        matched_patterns = []
        confidence_scores = {}

        for pattern in self.failure_patterns:
            matches, confidence = pattern.matches(signals_dict)
            if matches:
                matched_patterns.append(pattern)
                confidence_scores[pattern.pattern_id] = confidence

        # Sort by confidence
        matched_patterns.sort(key=lambda p: confidence_scores.get(p.pattern_id, 0), reverse=True)

        # Compute health score
        health_score = self._compute_health_score(process_signals, socket_signals, resource_signals)

        # Determine status
        status = self._determine_status(process_signals, socket_signals, health_score)

        # Determine recommended strategy
        if matched_patterns:
            recommended = matched_patterns[0].solution_strategy
            estimated_time = matched_patterns[0].avg_recovery_time_ms
        elif health_score > 0.8:
            recommended = RecoveryLevel.LEVEL_1_SOFT
            estimated_time = 10000
        elif health_score > 0.5:
            recommended = RecoveryLevel.LEVEL_2_RESET
            estimated_time = 30000
        else:
            recommended = RecoveryLevel.LEVEL_3_DEEP
            estimated_time = 60000

        # Identify blockers
        blockers = []
        if not resource_signals.sufficient_for_docker:
            if resource_signals.memory_available_gb < self.config.min_memory_gb:
                blockers.append(f"Insufficient memory: {resource_signals.memory_available_gb:.1f}GB")
            if resource_signals.disk_available_gb < self.config.min_disk_gb:
                blockers.append(f"Insufficient disk: {resource_signals.disk_available_gb:.1f}GB")

        diagnosis_time = int((time.time() - start_time) * 1000)

        diagnostic = DiagnosticResult(
            health_score=health_score,
            status=status,
            can_auto_heal=len(blockers) == 0 and self.config.enable_self_healing,
            process_signals=process_signals,
            socket_signals=socket_signals,
            resource_signals=resource_signals,
            state_signals=state_signals,
            matched_patterns=matched_patterns,
            confidence_scores=confidence_scores,
            recommended_strategy=recommended,
            estimated_recovery_ms=estimated_time,
            blockers=blockers,
            diagnosis_time_ms=diagnosis_time,
            timestamp=time.time(),
        )

        self._last_diagnostic = diagnostic

        if self.config.enable_verbose_logging:
            logger.debug(f"Diagnosis completed in {diagnosis_time}ms: {diagnostic.to_dict()}")

        return diagnostic

    async def _check_all_processes(self) -> Dict[str, ProcessSignal]:
        """Check all Docker-related processes"""

        if self.platform == 'darwin':
            process_map = MACOS_DOCKER_PROCESSES
        elif self.platform == 'linux':
            process_map = LINUX_DOCKER_PROCESSES
        elif self.platform == 'windows':
            process_map = WINDOWS_DOCKER_PROCESSES
        else:
            process_map = LINUX_DOCKER_PROCESSES

        signals = {}

        # Check each process in parallel
        async def check_process(name: str, priority: int) -> ProcessSignal:
            try:
                # Use psutil for more reliable process detection
                for proc in psutil.process_iter(['name', 'pid', 'cpu_percent', 'memory_info']):
                    try:
                        proc_name = proc.info['name']
                        if proc_name and (name.lower() in proc_name.lower() or proc_name.lower() in name.lower()):
                            mem_mb = proc.info['memory_info'].rss / (1024 * 1024) if proc.info['memory_info'] else 0
                            return ProcessSignal(
                                name=name,
                                running=True,
                                pid=proc.info['pid'],
                                cpu_percent=proc.info.get('cpu_percent'),
                                memory_mb=mem_mb,
                                priority=priority
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                return ProcessSignal(name=name, running=False, priority=priority)

            except Exception as e:
                logger.debug(f"Error checking process {name}: {e}")
                return ProcessSignal(name=name, running=False, priority=priority)

        tasks = [check_process(name, priority) for name, priority in process_map.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, ProcessSignal):
                # Use simplified key names
                key = result.name.replace('com.docker.', '').replace('.exe', '')
                signals[key] = result

        return signals

    async def _check_all_sockets(self) -> Dict[str, SocketSignal]:
        """Check all Docker socket paths"""

        socket_paths = [
            Path('/var/run/docker.sock'),
            Path.home() / '.docker' / 'run' / 'docker.sock',
            Path.home() / 'Library' / 'Containers' / 'com.docker.docker' / 'Data' / 'docker.sock',
        ]

        if self.platform == 'windows':
            socket_paths = [Path(r'\\.\pipe\docker_engine')]

        signals = {}

        async def check_socket(path: Path) -> SocketSignal:
            signal = SocketSignal(path=str(path), exists=False)

            try:
                if path.exists() or path.is_symlink():
                    signal.exists = True
                    signal.is_symlink = path.is_symlink()

                    if signal.is_symlink:
                        try:
                            signal.resolved_path = str(path.resolve())
                        except Exception:
                            pass

                    # Check permissions
                    signal.readable = os.access(path, os.R_OK)
                    signal.writable = os.access(path, os.W_OK)

                    # Check age
                    try:
                        stat = path.stat()
                        signal.age_seconds = time.time() - stat.st_mtime
                    except Exception:
                        pass

                    # Check if responsive (try docker info)
                    signal.responsive = await self._check_daemon_responsive()

            except Exception as e:
                logger.debug(f"Error checking socket {path}: {e}")

            return signal

        tasks = [check_socket(path) for path in socket_paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, SocketSignal):
                key = 'primary' if i == 0 else f'alt_{i}'
                signals[key] = result

        return signals

    async def _check_resources(self) -> ResourceSignal:
        """Check system resource availability"""

        try:
            # Memory
            mem = psutil.virtual_memory()
            memory_available_gb = mem.available / (1024 ** 3)
            memory_total_gb = mem.total / (1024 ** 3)
            memory_percent = mem.percent

            # Disk
            disk = psutil.disk_usage('/')
            disk_available_gb = disk.free / (1024 ** 3)
            disk_total_gb = disk.total / (1024 ** 3)
            disk_percent = disk.percent

            # CPU
            cpu_percent = psutil.cpu_percent(interval=0.1)

            # Memory pressure (macOS specific)
            memory_pressure = 'normal'
            if self.platform == 'darwin':
                try:
                    proc = await asyncio.create_subprocess_exec(
                        'memory_pressure',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                    output = stdout.decode().lower()

                    if 'critical' in output:
                        memory_pressure = 'critical'
                    elif 'warn' in output:
                        memory_pressure = 'warning'
                except Exception:
                    pass
            else:
                # Estimate from percentage
                if memory_percent > 90:
                    memory_pressure = 'critical'
                elif memory_percent > 80:
                    memory_pressure = 'warning'

            # Determine if sufficient
            sufficient = (
                memory_available_gb >= self.config.min_memory_gb and
                disk_available_gb >= self.config.min_disk_gb and
                cpu_percent < self.config.max_cpu_percent
            )

            return ResourceSignal(
                memory_available_gb=memory_available_gb,
                memory_total_gb=memory_total_gb,
                memory_percent_used=memory_percent,
                disk_available_gb=disk_available_gb,
                disk_total_gb=disk_total_gb,
                disk_percent_used=disk_percent,
                cpu_percent=cpu_percent,
                memory_pressure=memory_pressure,
                sufficient_for_docker=sufficient,
            )

        except Exception as e:
            logger.warning(f"Error checking resources: {e}")
            return ResourceSignal(
                memory_available_gb=8.0, memory_total_gb=16.0, memory_percent_used=50.0,
                disk_available_gb=50.0, disk_total_gb=500.0, disk_percent_used=90.0,
                cpu_percent=50.0, memory_pressure='normal', sufficient_for_docker=True
            )

    async def _check_state_files(self) -> StateSignal:
        """Check Docker state files"""

        signal = StateSignal(settings_exist=False, vm_disk_exists=False)

        if self.platform != 'darwin':
            return signal

        try:
            # Docker Desktop settings
            settings_dir = Path.home() / 'Library' / 'Group Containers' / 'group.com.docker'
            signal.settings_exist = settings_dir.exists()

            # VM disk
            vm_disk = Path.home() / 'Library' / 'Containers' / 'com.docker.docker' / 'Data' / 'vms' / '0' / 'data' / 'Docker.raw'
            if vm_disk.exists():
                signal.vm_disk_exists = True
                try:
                    signal.vm_disk_size_gb = vm_disk.stat().st_size / (1024 ** 3)
                except Exception:
                    pass

            # Lock files
            lock_patterns = [
                Path.home() / 'Library' / 'Containers' / 'com.docker.docker' / '.docker.lock',
                Path('/var/run/docker.pid'),
            ]
            for lock in lock_patterns:
                if lock.exists():
                    signal.lock_files.append(str(lock))

            # Crash reports
            crash_dir = Path.home() / 'Library' / 'Logs' / 'DiagnosticReports'
            if crash_dir.exists():
                recent_crashes = []
                cutoff = time.time() - 3600  # Last hour
                for f in crash_dir.glob('Docker*.crash'):
                    try:
                        if f.stat().st_mtime > cutoff:
                            recent_crashes.append(str(f))
                    except Exception:
                        pass
                signal.crash_reports = recent_crashes[:5]

            # Update in progress
            update_file = Path.home() / 'Library' / 'Group Containers' / 'group.com.docker' / '.docker-update-in-progress'
            signal.update_in_progress = update_file.exists()

        except Exception as e:
            logger.debug(f"Error checking state files: {e}")

        return signal

    def _compute_health_score(
        self,
        processes: Dict[str, ProcessSignal],
        sockets: Dict[str, SocketSignal],
        resources: ResourceSignal
    ) -> float:
        """Compute overall health score (0.0 - 1.0)"""

        score = 0.0
        weights = {
            'backend_running': 0.30,
            'socket_responsive': 0.30,
            'socket_exists': 0.10,
            'resources_sufficient': 0.15,
            'no_memory_pressure': 0.15,
        }

        # Backend running
        backend = processes.get('backend') or processes.get('Docker')
        if backend and backend.running:
            score += weights['backend_running']

        # Socket responsive
        primary_socket = sockets.get('primary')
        if primary_socket and primary_socket.responsive:
            score += weights['socket_responsive']
        elif primary_socket and primary_socket.exists:
            score += weights['socket_exists']

        # Resources
        if resources.sufficient_for_docker:
            score += weights['resources_sufficient']

        if resources.memory_pressure == 'normal':
            score += weights['no_memory_pressure']
        elif resources.memory_pressure == 'warning':
            score += weights['no_memory_pressure'] * 0.5

        return min(1.0, max(0.0, score))

    def _determine_status(
        self,
        processes: Dict[str, ProcessSignal],
        sockets: Dict[str, SocketSignal],
        health_score: float
    ) -> DaemonStatus:
        """Determine daemon status from signals"""

        backend = processes.get('backend') or processes.get('Docker')
        primary_socket = sockets.get('primary')

        if health_score >= 0.9:
            return DaemonStatus.RUNNING
        elif health_score >= 0.6:
            return DaemonStatus.DEGRADED
        elif backend and backend.running:
            if primary_socket and primary_socket.exists:
                return DaemonStatus.STARTING
            return DaemonStatus.DEGRADED
        elif primary_socket and primary_socket.exists:
            return DaemonStatus.INSTALLED_NOT_RUNNING
        else:
            return DaemonStatus.INSTALLED_NOT_RUNNING

    # =========================================================================
    # HEALTH CHECKS (Backward Compatible API)
    # =========================================================================

    async def check_daemon_health(self) -> DaemonHealth:
        """
        Check daemon health (backward compatible API).

        Internally uses the diagnostic engine for comprehensive analysis.
        """
        await self.initialize()

        diagnostic = await self.diagnose()

        # Map diagnostic to DaemonHealth
        backend = diagnostic.process_signals.get('backend') or diagnostic.process_signals.get('Docker')
        primary_socket = diagnostic.socket_signals.get('primary')

        self.health = DaemonHealth(
            status=diagnostic.status,
            daemon_responsive=primary_socket.responsive if primary_socket else False,
            api_accessible=primary_socket.responsive if primary_socket else False,
            containers_queryable=primary_socket.responsive if primary_socket else False,
            socket_exists=primary_socket.exists if primary_socket else False,
            process_running=backend.running if backend else False,
            last_check_timestamp=time.time(),
            diagnostic_result=diagnostic,
        )

        # Update cross-repo state
        await self.event_emitter.update_state(self.health, diagnostic)

        return self.health

    async def _check_daemon_responsive(self) -> bool:
        """Check if daemon responds to 'docker info'"""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', 'info',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.health_check_timeout
            )

            return proc.returncode == 0

        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def check_installation(self) -> bool:
        """Check if Docker is installed"""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', '--version',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                version = stdout.decode().strip()
                logger.info(f"Docker installed: {version}")
                return True
            return False

        except Exception:
            return False

    # =========================================================================
    # SELF-HEALING RECOVERY
    # =========================================================================

    async def _level_1_soft_recovery(self, diagnostic: DiagnosticResult) -> RecoveryResult:
        """
        Level 1: Soft recovery - minimal intervention.

        Actions:
        1. Clear stale PID files
        2. Prune stopped containers
        3. Prune dangling images
        4. Gentle Docker Desktop start
        """
        start_time = time.time()
        actions_taken = []

        self._report_progress("Level 1: Soft recovery starting...")

        try:
            # 1. Clear stale PID files
            if await self._clear_stale_pid_files():
                actions_taken.append("cleared_stale_pids")

            # 2. Resource cleanup (if Docker is partially running)
            if diagnostic.health_score > 0.3:
                cleanup_results = await asyncio.gather(
                    self._prune_stopped_containers(),
                    self._prune_dangling_images(),
                    return_exceptions=True
                )

                if cleanup_results[0] and not isinstance(cleanup_results[0], Exception):
                    actions_taken.append("pruned_containers")
                if cleanup_results[1] and not isinstance(cleanup_results[1], Exception):
                    actions_taken.append("pruned_images")

            # 3. Start Docker Desktop
            self._report_progress("Starting Docker Desktop...")
            if await self._start_docker_desktop():
                actions_taken.append("started_docker")

                # 4. Wait for readiness
                timeout_config = await self.timeout_calculator.calculate_optimal_timeout(
                    diagnostic, RecoveryLevel.LEVEL_1_SOFT
                ) if self.timeout_calculator else TimeoutConfig(
                    total_timeout_seconds=60, poll_interval_seconds=2.0,
                    progress_update_interval_seconds=5.0, factors={}
                )

                if await self._wait_for_daemon_ready(timeout_config):
                    duration = int((time.time() - start_time) * 1000)
                    return RecoveryResult(
                        success=True,
                        level=RecoveryLevel.LEVEL_1_SOFT,
                        actions_taken=actions_taken,
                        duration_ms=duration,
                    )

            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_1_SOFT,
                actions_taken=actions_taken,
                duration_ms=duration,
                error="Docker did not become ready",
                next_recommended_level=RecoveryLevel.LEVEL_2_RESET,
            )

        except Exception as e:
            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_1_SOFT,
                actions_taken=actions_taken,
                duration_ms=duration,
                error=str(e),
                next_recommended_level=RecoveryLevel.LEVEL_2_RESET,
            )

    async def _level_2_reset_recovery(self, diagnostic: DiagnosticResult) -> RecoveryResult:
        """
        Level 2: Reset recovery - restart Docker Desktop.

        Actions:
        1. Gracefully quit Docker Desktop
        2. Wait for processes to terminate
        3. Kill zombie processes
        4. Clear stale sockets
        5. Fresh start
        """
        start_time = time.time()
        actions_taken = []

        self._report_progress("Level 2: Reset recovery starting...")

        try:
            # 1. Quit Docker Desktop gracefully
            self._report_progress("Quitting Docker Desktop...")
            if await self._quit_docker_desktop():
                actions_taken.append("quit_docker")

            # 2. Wait for processes to terminate
            await asyncio.sleep(3.0)

            # 3. Kill zombie processes
            killed = await self._kill_docker_processes()
            if killed:
                actions_taken.append(f"killed_{killed}_processes")

            # 4. Clear stale sockets
            if await self._clear_stale_sockets():
                actions_taken.append("cleared_sockets")

            # 5. Fresh start
            self._report_progress("Starting Docker Desktop fresh...")
            await asyncio.sleep(2.0)  # Give system time to clean up

            if await self._start_docker_desktop():
                actions_taken.append("started_docker")

                timeout_config = await self.timeout_calculator.calculate_optimal_timeout(
                    diagnostic, RecoveryLevel.LEVEL_2_RESET
                ) if self.timeout_calculator else TimeoutConfig(
                    total_timeout_seconds=90, poll_interval_seconds=2.0,
                    progress_update_interval_seconds=5.0, factors={}
                )

                if await self._wait_for_daemon_ready(timeout_config):
                    duration = int((time.time() - start_time) * 1000)
                    return RecoveryResult(
                        success=True,
                        level=RecoveryLevel.LEVEL_2_RESET,
                        actions_taken=actions_taken,
                        duration_ms=duration,
                    )

            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_2_RESET,
                actions_taken=actions_taken,
                duration_ms=duration,
                error="Docker did not become ready after reset",
                next_recommended_level=RecoveryLevel.LEVEL_3_DEEP,
            )

        except Exception as e:
            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_2_RESET,
                actions_taken=actions_taken,
                duration_ms=duration,
                error=str(e),
                next_recommended_level=RecoveryLevel.LEVEL_3_DEEP,
            )

    async def _level_3_deep_recovery(self, diagnostic: DiagnosticResult) -> RecoveryResult:
        """
        Level 3: Deep recovery - reset Docker state.

        Actions:
        1. All Level 2 actions
        2. Clear Docker Desktop settings cache
        3. Remove lock files
        4. Clear crash markers
        5. Extended restart
        """
        start_time = time.time()
        actions_taken = []

        self._report_progress("Level 3: Deep recovery starting...")

        try:
            # 1. First do level 2 quit and kill
            await self._quit_docker_desktop()
            actions_taken.append("quit_docker")

            await asyncio.sleep(3.0)

            killed = await self._kill_docker_processes()
            if killed:
                actions_taken.append(f"killed_{killed}_processes")

            # 2. Deep cleaning
            self._report_progress("Deep cleaning Docker state...")

            # Clear settings cache (but not user data)
            if await self._clear_settings_cache():
                actions_taken.append("cleared_cache")

            # Remove lock files
            if await self._remove_lock_files():
                actions_taken.append("removed_locks")

            # Clear crash markers
            if await self._clear_crash_markers():
                actions_taken.append("cleared_crash_markers")

            # Clear stale sockets
            if await self._clear_stale_sockets():
                actions_taken.append("cleared_sockets")

            # 3. Extended startup wait
            self._report_progress("Starting Docker Desktop with extended timeout...")
            await asyncio.sleep(3.0)

            if await self._start_docker_desktop():
                actions_taken.append("started_docker")

                timeout_config = await self.timeout_calculator.calculate_optimal_timeout(
                    diagnostic, RecoveryLevel.LEVEL_3_DEEP
                ) if self.timeout_calculator else TimeoutConfig(
                    total_timeout_seconds=120, poll_interval_seconds=3.0,
                    progress_update_interval_seconds=5.0, factors={}
                )

                if await self._wait_for_daemon_ready(timeout_config):
                    duration = int((time.time() - start_time) * 1000)
                    return RecoveryResult(
                        success=True,
                        level=RecoveryLevel.LEVEL_3_DEEP,
                        actions_taken=actions_taken,
                        duration_ms=duration,
                    )

            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_3_DEEP,
                actions_taken=actions_taken,
                duration_ms=duration,
                error="Docker did not become ready after deep recovery",
                next_recommended_level=RecoveryLevel.LEVEL_4_NUCLEAR,
            )

        except Exception as e:
            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_3_DEEP,
                actions_taken=actions_taken,
                duration_ms=duration,
                error=str(e),
                next_recommended_level=RecoveryLevel.LEVEL_4_NUCLEAR,
            )

    async def _level_4_nuclear_recovery(
        self,
        diagnostic: DiagnosticResult,
        user_confirmed: bool = False
    ) -> RecoveryResult:
        """
        Level 4: Nuclear recovery - factory reset (DESTRUCTIVE).

        WARNING: This removes all containers, images, and volumes!

        Actions:
        1. User confirmation required
        2. Full Docker Desktop quit
        3. Remove all Docker data
        4. Fresh initialization
        """
        if not user_confirmed and self.config.require_confirmation_level_4:
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_4_NUCLEAR,
                actions_taken=[],
                duration_ms=0,
                error="User confirmation required for nuclear recovery (destructive)",
                requires_confirmation=True,
            )

        start_time = time.time()
        actions_taken = []

        self._report_progress("Level 4: NUCLEAR recovery starting (this will delete all Docker data)...")

        try:
            # 1. Quit and kill everything
            await self._quit_docker_desktop()
            actions_taken.append("quit_docker")

            await asyncio.sleep(5.0)

            await self._kill_docker_processes(force=True)
            actions_taken.append("force_killed_processes")

            # 2. Remove Docker data directories
            self._report_progress("Removing Docker data...")

            if self.platform == 'darwin':
                data_dirs = [
                    Path.home() / 'Library' / 'Containers' / 'com.docker.docker',
                    Path.home() / 'Library' / 'Group Containers' / 'group.com.docker',
                    Path.home() / '.docker',
                ]

                for data_dir in data_dirs:
                    if data_dir.exists():
                        try:
                            shutil.rmtree(data_dir)
                            actions_taken.append(f"removed_{data_dir.name}")
                        except Exception as e:
                            logger.warning(f"Failed to remove {data_dir}: {e}")

            # 3. Fresh start
            self._report_progress("Starting Docker Desktop fresh...")
            await asyncio.sleep(5.0)

            if await self._start_docker_desktop():
                actions_taken.append("started_docker")

                # Extended timeout for first-time setup
                timeout_config = TimeoutConfig(
                    total_timeout_seconds=180,
                    poll_interval_seconds=5.0,
                    progress_update_interval_seconds=10.0,
                    factors={'nuclear_recovery': True}
                )

                if await self._wait_for_daemon_ready(timeout_config):
                    duration = int((time.time() - start_time) * 1000)
                    return RecoveryResult(
                        success=True,
                        level=RecoveryLevel.LEVEL_4_NUCLEAR,
                        actions_taken=actions_taken,
                        duration_ms=duration,
                    )

            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_4_NUCLEAR,
                actions_taken=actions_taken,
                duration_ms=duration,
                error="Docker did not start after nuclear recovery - manual intervention required",
            )

        except Exception as e:
            duration = int((time.time() - start_time) * 1000)
            return RecoveryResult(
                success=False,
                level=RecoveryLevel.LEVEL_4_NUCLEAR,
                actions_taken=actions_taken,
                duration_ms=duration,
                error=str(e),
            )

    # =========================================================================
    # RECOVERY HELPERS
    # =========================================================================

    async def _clear_stale_pid_files(self) -> bool:
        """Clear stale PID files"""
        cleared = False
        pid_files = [
            Path('/var/run/docker.pid'),
            Path.home() / '.docker' / 'docker.pid',
        ]

        for pid_file in pid_files:
            try:
                if pid_file.exists():
                    # Check if PID is actually running
                    pid = int(pid_file.read_text().strip())
                    if not psutil.pid_exists(pid):
                        pid_file.unlink()
                        cleared = True
                        logger.info(f"Cleared stale PID file: {pid_file}")
            except Exception as e:
                logger.debug(f"Error clearing PID file {pid_file}: {e}")

        return cleared

    async def _prune_stopped_containers(self) -> bool:
        """Prune stopped containers"""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', 'container', 'prune', '-f',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            return proc.returncode == 0
        except Exception:
            return False

    async def _prune_dangling_images(self) -> bool:
        """Prune dangling images"""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', 'image', 'prune', '-f',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            return proc.returncode == 0
        except Exception:
            return False

    async def _start_docker_desktop(self) -> bool:
        """Start Docker Desktop application"""
        try:
            if self.platform == 'darwin':
                app_path = self.config.docker_app_path_macos

                if not Path(app_path).exists():
                    logger.error(f"Docker.app not found at {app_path}")
                    return False

                proc = await asyncio.create_subprocess_exec(
                    'open', '-a', app_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            elif self.platform == 'linux':
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'systemctl', 'start', 'docker',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            return False

        except Exception as e:
            logger.error(f"Error starting Docker: {e}")
            return False

    async def _quit_docker_desktop(self) -> bool:
        """Gracefully quit Docker Desktop"""
        try:
            if self.platform == 'darwin':
                proc = await asyncio.create_subprocess_exec(
                    'osascript', '-e', 'quit app "Docker"',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return True

            elif self.platform == 'linux':
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'systemctl', 'stop', 'docker',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            return False

        except Exception as e:
            logger.debug(f"Error quitting Docker: {e}")
            return False

    async def _kill_docker_processes(self, force: bool = False) -> int:
        """Kill Docker processes"""
        killed = 0

        if self.platform == 'darwin':
            process_names = list(MACOS_DOCKER_PROCESSES.keys())
        elif self.platform == 'linux':
            process_names = list(LINUX_DOCKER_PROCESSES.keys())
        else:
            process_names = list(WINDOWS_DOCKER_PROCESSES.keys())

        for proc in psutil.process_iter(['name', 'pid']):
            try:
                proc_name = proc.info['name']
                if proc_name and any(n.lower() in proc_name.lower() for n in process_names):
                    if force:
                        proc.kill()
                    else:
                        proc.terminate()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if killed > 0:
            await asyncio.sleep(2.0)  # Wait for processes to terminate

        return killed

    async def _clear_stale_sockets(self) -> bool:
        """Clear stale Docker sockets"""
        cleared = False

        socket_paths = [
            Path.home() / '.docker' / 'run' / 'docker.sock',
        ]

        for socket_path in socket_paths:
            try:
                if socket_path.exists() and not socket_path.is_socket():
                    socket_path.unlink()
                    cleared = True
            except Exception as e:
                logger.debug(f"Error clearing socket {socket_path}: {e}")

        return cleared

    async def _clear_settings_cache(self) -> bool:
        """Clear Docker Desktop settings cache (not user data)"""
        if self.platform != 'darwin':
            return False

        cache_dirs = [
            Path.home() / 'Library' / 'Caches' / 'com.docker.docker',
        ]

        cleared = False
        for cache_dir in cache_dirs:
            try:
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
                    cleared = True
            except Exception as e:
                logger.debug(f"Error clearing cache {cache_dir}: {e}")

        return cleared

    async def _remove_lock_files(self) -> bool:
        """Remove Docker lock files"""
        removed = False

        lock_files = [
            Path.home() / 'Library' / 'Containers' / 'com.docker.docker' / '.docker.lock',
            Path('/var/run/docker.pid'),
        ]

        for lock_file in lock_files:
            try:
                if lock_file.exists():
                    lock_file.unlink()
                    removed = True
                    logger.info(f"Removed lock file: {lock_file}")
            except Exception as e:
                logger.debug(f"Error removing lock file {lock_file}: {e}")

        return removed

    async def _clear_crash_markers(self) -> bool:
        """Clear crash marker files"""
        if self.platform != 'darwin':
            return False

        marker_files = [
            Path.home() / 'Library' / 'Group Containers' / 'group.com.docker' / '.docker-crashed',
            Path.home() / 'Library' / 'Group Containers' / 'group.com.docker' / '.docker-update-in-progress',
        ]

        cleared = False
        for marker in marker_files:
            try:
                if marker.exists():
                    marker.unlink()
                    cleared = True
            except Exception as e:
                logger.debug(f"Error clearing marker {marker}: {e}")

        return cleared

    async def _wait_for_daemon_ready(self, timeout_config: TimeoutConfig) -> bool:
        """Wait for daemon to become ready"""
        start_time = time.time()
        check_count = 0

        while (time.time() - start_time) < timeout_config.total_timeout_seconds:
            check_count += 1

            # Check daemon health
            responsive = await self._check_daemon_responsive()

            if responsive:
                elapsed = time.time() - start_time
                self.health.startup_time_ms = int(elapsed * 1000)
                logger.info(f"Docker daemon ready in {elapsed:.1f}s")
                return True

            # Progress update
            if check_count % max(1, int(timeout_config.progress_update_interval_seconds / timeout_config.poll_interval_seconds)) == 0:
                elapsed = time.time() - start_time
                remaining = timeout_config.total_timeout_seconds - elapsed
                self._report_progress(f"Waiting for Docker... ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)")

            await asyncio.sleep(timeout_config.poll_interval_seconds)

        logger.warning(f"Timeout waiting for Docker daemon ({timeout_config.total_timeout_seconds}s)")
        return False

    # =========================================================================
    # MAIN API
    # =========================================================================

    async def start_daemon(self) -> bool:
        """
        Start Docker daemon with intelligent self-healing.

        Uses progressive recovery levels and learning from history.
        """
        await self.initialize()

        # Check circuit breaker
        can_attempt, reason = await self.circuit_breaker.should_attempt()
        if not can_attempt:
            logger.warning(f"Docker start blocked by circuit breaker: {reason}")
            self._report_progress(f"Circuit breaker active: {reason}")
            return False

        # Check if already running
        diagnostic = await self.diagnose()

        if diagnostic.health_score >= 0.9:
            logger.info("Docker daemon already running and healthy")
            await self.circuit_breaker.record_success()
            return True

        # Emit starting event
        await self.event_emitter.emit(DockerStateEvent.STARTING, {
            'health_score': diagnostic.health_score,
            'matched_patterns': [p.pattern_id for p in diagnostic.matched_patterns],
        })

        # Get prediction if available
        prediction = None
        if self.predictive_engine:
            prediction = await self.predictive_engine.predict_startup(diagnostic)
            logger.info(
                f"Startup prediction: {prediction.predicted_success_probability:.0%} success, "
                f"recommended: {prediction.recommended_strategy.name}"
            )

            if prediction.risk_factors:
                logger.warning(f"Risk factors: {', '.join(prediction.risk_factors)}")

        # Determine starting level
        if diagnostic.blockers:
            logger.error(f"Cannot auto-heal: {', '.join(diagnostic.blockers)}")
            return False

        start_level = prediction.recommended_strategy if prediction else diagnostic.recommended_strategy

        # Don't exceed max healing level
        max_level = RecoveryLevel(min(self.config.max_healing_level, 3))
        if start_level.value > max_level.value:
            start_level = max_level

        # Progressive recovery
        current_level = start_level
        recovery_result: Optional[RecoveryResult] = None

        while current_level.value <= max_level.value:
            logger.info(f"Attempting {current_level.name} recovery...")

            await self.event_emitter.emit(DockerStateEvent.RECOVERY_STARTED, {
                'level': current_level.name,
                'attempt': current_level.value,
            })

            if current_level == RecoveryLevel.LEVEL_1_SOFT:
                recovery_result = await self._level_1_soft_recovery(diagnostic)
            elif current_level == RecoveryLevel.LEVEL_2_RESET:
                recovery_result = await self._level_2_reset_recovery(diagnostic)
            elif current_level == RecoveryLevel.LEVEL_3_DEEP:
                recovery_result = await self._level_3_deep_recovery(diagnostic)
            elif current_level == RecoveryLevel.LEVEL_4_NUCLEAR:
                recovery_result = await self._level_4_nuclear_recovery(diagnostic)
                break  # Don't escalate beyond nuclear

            if recovery_result.success:
                # Success!
                logger.info(
                    f"Docker started successfully with {current_level.name} "
                    f"in {recovery_result.duration_ms}ms"
                )

                await self.circuit_breaker.record_success()

                await self.event_emitter.emit(DockerStateEvent.RECOVERY_SUCCEEDED, {
                    'level': current_level.name,
                    'duration_ms': recovery_result.duration_ms,
                    'actions': recovery_result.actions_taken,
                })

                # Record for learning
                if self.config.enable_learning:
                    await self.learning_db.record_attempt(
                        diagnostic=diagnostic,
                        recovery_level=current_level,
                        success=True,
                        startup_time_ms=recovery_result.duration_ms,
                        prediction_probability=prediction.predicted_success_probability if prediction else None,
                    )

                return True

            # Failed - escalate
            logger.warning(
                f"{current_level.name} failed: {recovery_result.error}"
            )

            await self.event_emitter.emit(DockerStateEvent.RECOVERY_LEVEL_ESCALATED, {
                'from_level': current_level.name,
                'to_level': recovery_result.next_recommended_level.name if recovery_result.next_recommended_level else None,
                'error': recovery_result.error,
            })

            if recovery_result.next_recommended_level:
                current_level = recovery_result.next_recommended_level
            else:
                break

            # Re-diagnose before next attempt
            diagnostic = await self.diagnose()

        # All attempts failed
        logger.error(f"All recovery attempts failed")

        await self.circuit_breaker.record_failure(
            recovery_result.error if recovery_result else "Unknown error"
        )

        await self.event_emitter.emit(DockerStateEvent.RECOVERY_FAILED, {
            'final_level': current_level.name,
            'error': recovery_result.error if recovery_result else "Unknown error",
        })

        # Record for learning
        if self.config.enable_learning and recovery_result:
            await self.learning_db.record_attempt(
                diagnostic=diagnostic,
                recovery_level=current_level,
                success=False,
                startup_time_ms=recovery_result.duration_ms,
                error_message=recovery_result.error,
                prediction_probability=prediction.predicted_success_probability if prediction else None,
            )

        return False

    async def ensure_daemon_running(
        self,
        auto_start: bool = True,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Ensure Docker daemon is running (backward compatible API).

        Args:
            auto_start: Whether to automatically start Docker if not running
            timeout: Maximum time to wait (overrides config)
            max_retries: Maximum retry attempts (overrides config)

        Returns:
            Status dictionary with daemon state
        """
        await self.initialize()

        # Override config if parameters provided
        if timeout is not None:
            self.config.max_startup_wait_seconds = int(timeout)
        if max_retries is not None:
            self.config.max_retry_attempts = max_retries

        status = {
            "installed": False,
            "daemon_running": False,
            "version": None,
            "started_automatically": False,
            "startup_time_ms": None,
            "error": None,
            "platform": self.platform,
            "self_healing_enabled": self.config.enable_self_healing,
            "circuit_breaker": self.circuit_breaker.get_state(),
        }

        logger.info(f"Docker Daemon Manager v{self.VERSION} - Status Check")
        self._report_progress("Checking Docker installation...")

        # Check installation
        if not await self.check_installation():
            status["error"] = "Docker not installed"
            logger.warning("Docker not installed")
            return status

        status["installed"] = True

        # Get version
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', '--version',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0:
                status["version"] = stdout.decode().strip()
        except Exception:
            pass

        # Diagnose current state
        diagnostic = await self.diagnose()

        if diagnostic.health_score >= 0.9:
            status["daemon_running"] = True
            logger.info("Docker daemon is running and healthy")
            return status

        # Not running - attempt auto-start?
        if not auto_start:
            status["error"] = "Docker daemon not running (auto-start disabled)"
            logger.warning(status["error"])
            return status

        logger.info("Docker daemon not running - attempting self-healing startup")
        self._report_progress("Docker not running - starting with self-healing...")

        if await self.start_daemon():
            status["daemon_running"] = True
            status["started_automatically"] = True
            status["startup_time_ms"] = self.health.startup_time_ms
            logger.info(f"Docker daemon started ({self.health.startup_time_ms}ms)")
        else:
            status["error"] = self.health.error_message or "Failed to start Docker daemon"
            logger.error(f"Failed to start Docker: {status['error']}")

        return status

    async def stop_daemon(self) -> bool:
        """Stop Docker daemon gracefully"""
        logger.info("Stopping Docker daemon...")

        await self.event_emitter.emit(DockerStateEvent.STOPPING, {})

        success = await self._quit_docker_desktop()

        if success:
            await self.event_emitter.emit(DockerStateEvent.STOPPED, {})

        return success

    def get_health(self) -> DaemonHealth:
        """Get current daemon health"""
        return self.health

    def get_status(self) -> Dict[str, Any]:
        """Get current daemon status"""
        return {
            "installed": self.health.status != DaemonStatus.NOT_INSTALLED,
            "daemon_running": self.health.is_healthy(),
            "version": None,
            "startup_time_ms": self.health.startup_time_ms,
            "error": self.health.error_message,
            "platform": self.platform,
            "circuit_breaker": self.circuit_breaker.get_state(),
        }

    def get_status_emoji(self) -> str:
        """Get formatted status string with emoji"""
        if self.health.is_healthy():
            return "Docker: Running"
        elif self.health.status == DaemonStatus.NOT_INSTALLED:
            return "Docker: Not installed"
        elif self.health.status == DaemonStatus.RECOVERING:
            return "Docker: Recovering..."
        else:
            error = self.health.error_message or "Not running"
            return f"Docker: {error}"

    def _report_progress(self, message: str):
        """Report progress via callback"""
        if self.progress_callback:
            try:
                self.progress_callback(message)
            except Exception as e:
                logger.debug(f"Progress callback error: {e}")


# =============================================================================
# FACTORY FUNCTION
# =============================================================================

async def create_docker_manager(
    config: Optional[DockerConfig] = None,
    progress_callback: Optional[Callable[[str], None]] = None
) -> DockerDaemonManager:
    """
    Create and initialize Docker daemon manager.

    Args:
        config: Optional configuration (uses environment if not provided)
        progress_callback: Optional callback for progress updates

    Returns:
        Initialized DockerDaemonManager
    """
    manager = DockerDaemonManager(config, progress_callback)
    await manager.initialize()

    # Initial health check
    await manager.check_daemon_health()

    return manager
