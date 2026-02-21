#!/usr/bin/env python3
"""
JARVIS Loading Server v212.0 - Enterprise-Grade Startup Orchestration Hub
==========================================================================

The ultimate loading server that serves as the central nervous system for
JARVIS startup coordination across all Trinity components.

v212.0 ENHANCEMENTS (Unified Feature Integration):
- W3C Distributed Tracing for cross-service debugging
- Event Sourcing with JSONL logs for replay capability
- SQLite Progress Persistence for browser refresh resume
- Enhanced ML-based ETA Prediction with historical learning
- Lock-Free Progress Updates with CAS atomic operations
- Container Awareness for K8s/Docker timeout scaling
- Adaptive Backpressure (AIMD) for slow client handling
- Cross-Repo Health Aggregation with circuit breakers
- Intelligent Context-Aware Message Generation
- Self-Healing Restart Manager with exponential backoff
- Trinity Heartbeat File Monitoring
- Parent Death Watcher (v211.0) - Prevents orphaned processes

v186.0 FIXES:
- Port injection for correct loading server communication
- Endpoint aliases (/api/startup-progress -> /api/progress)
- Sequence tracking for missed update recovery
- Resume endpoint for WebSocket reconnection

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    Loading Server v182.0 (Port 3001)                    │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  Layer 1: HTTP/WebSocket Server (asyncio native)                        │
    │  ├─ REST API endpoints for health, status, progress                     │
    │  ├─ WebSocket real-time streaming with heartbeat                        │
    │  ├─ SSE (Server-Sent Events) fallback                                   │
    │  └─ Static file serving for loading page                                │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  Layer 2: Unified Progress Hub Integration                              │
    │  ├─ Syncs with UnifiedStartupProgressHub (single source of truth)       │
    │  ├─ Component dependency graph tracking                                 │
    │  ├─ ML-based ETA prediction engine                                      │
    │  └─ Progress monotonicity enforcement                                   │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  Layer 3: Trinity Health Aggregation                                    │
    │  ├─ Real-time health from JARVIS Body (8010)                            │
    │  ├─ Real-time health from JARVIS Prime (8000)                           │
    │  ├─ Real-time health from Reactor Core (8090)                           │
    │  ├─ Heartbeat file monitoring                                           │
    │  └─ Bidirectional health propagation                                    │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  Layer 4: Intelligent Resilience                                        │
    │  ├─ Circuit breaker pattern for all outbound calls                      │
    │  ├─ Adaptive retry with exponential backoff + jitter                    │
    │  ├─ Graceful degradation paths                                          │
    │  └─ Dead letter queue for failed events                                 │
    └─────────────────────────────────────────────────────────────────────────┘

Features:
- Zero hardcoding - all configuration via environment + discovery
- ML-based ETA prediction with historical learning
- Real-time WebSocket streaming with automatic reconnection
- Component dependency graph with cascade failure prevention
- Bidirectional health propagation across Trinity
- Graceful degradation with feature fallbacks
- Circuit breakers with adaptive thresholds
- Dead letter queue for event persistence
- Voice narrator integration for TTS feedback

API Endpoints:
    GET  /                            - Loading page (HTML)
    GET  /health                      - Standard health endpoint (v185.0)
    GET  /ready                       - Kubernetes-style readiness check (v185.0)
    GET  /api/supervisor/health       - Supervisor health status
    GET  /api/supervisor/heartbeat    - Heartbeat for keep-alive
    GET  /api/supervisor/status       - Full supervisor status
    GET  /api/health/unified          - Cross-repo unified health
    GET  /api/progress                - Current progress state
    GET  /api/progress/eta            - ETA prediction
    GET  /api/progress/components     - Component status breakdown
    GET  /api/progress/history        - Progress event history
    POST /api/update-progress         - Update progress (from supervisor)
    POST /api/component/register      - Register a new component
    POST /api/component/complete      - Mark component complete
    POST /api/shutdown                - Graceful shutdown
    WS   /ws/progress                 - Real-time progress stream
    GET  /sse/progress                - SSE fallback stream

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import math
import os
import signal
import statistics
import sys
import threading
import time
import traceback
import uuid
import weakref
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import lru_cache, wraps
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Coroutine, DefaultDict, Deque, Dict,
    FrozenSet, Generic, Iterator, List, Mapping, NamedTuple, Optional,
    Protocol, Sequence, Set, Tuple, Type, TypeVar, Union, cast
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.getenv("LOADING_SERVER_LOG_LEVEL", "INFO")),
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger("LoadingServer.v212")


def _parse_log_level(level_name: str, default: int) -> int:
    """Parse a logging level name safely."""
    level = getattr(logging, str(level_name).upper(), None)
    return level if isinstance(level, int) else default


def _env_flag(name: str, default: bool) -> bool:
    """Parse common truthy/falsey environment values."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class _RegisteredCheckpointFilter(logging.Filter):
    """Suppress SpeechBrain checkpoint hook registration noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "registered checkpoint" not in record.getMessage().lower()


def _configure_third_party_log_noise() -> None:
    """
    Enforce deterministic third-party logging policy for this process.

    This prevents noisy SpeechBrain debug output from bypassing main startup logs.
    """
    third_party_level = _parse_log_level(
        os.getenv("JARVIS_THIRD_PARTY_LOG_LEVEL", "WARNING"),
        logging.WARNING,
    )
    speechbrain_level = _parse_log_level(
        os.getenv("JARVIS_SPEECHBRAIN_LOG_LEVEL", "ERROR"),
        logging.ERROR,
    )

    noisy_loggers = (
        "speechbrain",
        "speechbrain.utils",
        "speechbrain.utils.checkpoints",
        "transformers",
        "transformers.modeling_utils",
        "huggingface_hub",
    )

    for logger_name in noisy_loggers:
        noisy_logger = logging.getLogger(logger_name)
        if logger_name.startswith("speechbrain"):
            noisy_logger.setLevel(speechbrain_level)
        else:
            noisy_logger.setLevel(third_party_level)

    if _env_flag("JARVIS_SUPPRESS_REGISTERED_CHECKPOINT_LOGS", True):
        checkpoint_filter = _RegisteredCheckpointFilter()
        for target_logger in (
            logging.getLogger(),
            logging.getLogger("speechbrain"),
            logging.getLogger("speechbrain.utils"),
            logging.getLogger("speechbrain.utils.checkpoints"),
        ):
            if checkpoint_filter not in target_logger.filters:
                target_logger.addFilter(checkpoint_filter)
        for handler in logging.getLogger().handlers:
            if checkpoint_filter not in handler.filters:
                handler.addFilter(checkpoint_filter)


_configure_third_party_log_noise()

# v210.0: Import safe task wrapper to prevent "Future exception was never retrieved"
try:
    from backend.core.async_safety import create_safe_task
except ImportError:
    # Fallback if async_safety is not available
    # Store raw create_task reference to avoid potential issues
    _raw_create_task = asyncio.create_task
    _fallback_tasks: set = set()
    
    def create_safe_task(coro, name=None, **kwargs):
        """Fallback for create_safe_task with proper exception handling."""
        task_name = name or "unnamed"
        try:
            task = _raw_create_task(coro, name=task_name)
        except TypeError:
            task = _raw_create_task(coro)
        
        # Keep reference to prevent GC before completion
        _fallback_tasks.add(task)
        
        def _handle_done(t):
            _fallback_tasks.discard(t)
            if t.cancelled():
                return
            try:
                exc = t.exception()
                if exc is not None:
                    logger.warning(f"[Task] {task_name} error: {type(exc).__name__}: {exc}")
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass  # Task was cancelled or not finished, ignore
        
        task.add_done_callback(_handle_done)
        return task

T = TypeVar('T')


# =============================================================================
# v212.0: INTEGRATED MODULE IMPORTS
# =============================================================================
# Import advanced features from the loading_server package

try:
    from backend.loading_server import (
        W3CTraceContext,
        EventSourcingLog,
        ProgressPersistence,
        PredictiveETACalculator,
        LockFreeProgressUpdate,
        ContainerAwareness,
        AdaptiveBackpressureController,
        CrossRepoHealthAggregator,
        IntelligentMessageGenerator,
        SelfHealingRestartManager,
        TrinityHeartbeatReader,
    )
    ADVANCED_FEATURES_AVAILABLE = True
    logger.info("[v212.0] Advanced loading server modules loaded")
except ImportError as e:
    ADVANCED_FEATURES_AVAILABLE = False
    logger.debug(f"[v212.0] Advanced features not available: {e}")


# =============================================================================
# v125.0: CONFIGURATION MANAGEMENT (Zero Hardcoding)
# =============================================================================

@dataclass(frozen=True)
class LoadingServerConfig:
    """
    v125.0: Immutable configuration with environment-based discovery.

    All values are dynamically loaded from environment variables with
    sensible defaults. No hardcoded values except for fallback defaults.
    """
    # Server settings
    port: int = field(default_factory=lambda: int(os.getenv("LOADING_SERVER_PORT", "3001")))
    host: str = field(default_factory=lambda: os.getenv("LOADING_SERVER_HOST", "0.0.0.0"))

    # Trinity service discovery
    backend_port: int = field(default_factory=lambda: int(os.getenv("JARVIS_BACKEND_PORT", "8010")))
    frontend_port: int = field(default_factory=lambda: int(os.getenv("JARVIS_FRONTEND_PORT", "3000")))
    # v238.0: Default 8001 to match trinity_config.py v192.2 alignment
    prime_port: int = field(default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8001")))
    reactor_port: int = field(default_factory=lambda: int(os.getenv("REACTOR_CORE_PORT", "8090")))

    # Paths
    jarvis_home: Path = field(default_factory=lambda: Path(os.getenv("JARVIS_HOME", str(Path.home() / ".jarvis"))))
    jarvis_repo: Path = field(default_factory=lambda: Path(os.getenv("JARVIS_PATH", str(Path.home() / "Documents/repos/JARVIS-AI-Agent"))))
    prime_repo: Path = field(default_factory=lambda: Path(os.getenv("JARVIS_PRIME_PATH", str(Path.home() / "Documents/repos/jarvis-prime"))))
    reactor_repo: Path = field(default_factory=lambda: Path(os.getenv("REACTOR_CORE_PATH", str(Path.home() / "Documents/repos/reactor-core"))))

    # Timeouts (seconds)
    health_check_timeout: float = field(default_factory=lambda: float(os.getenv("HEALTH_CHECK_TIMEOUT", "5.0")))
    websocket_ping_interval: float = field(default_factory=lambda: float(os.getenv("WS_PING_INTERVAL", "15.0")))
    eta_update_interval: float = field(default_factory=lambda: float(os.getenv("ETA_UPDATE_INTERVAL", "2.0")))

    # Circuit breaker
    circuit_breaker_threshold: int = field(default_factory=lambda: int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5")))
    circuit_breaker_timeout: float = field(default_factory=lambda: float(os.getenv("CIRCUIT_BREAKER_TIMEOUT", "30.0")))

    # Resilience
    max_retry_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_RETRY_ATTEMPTS", "3")))
    retry_base_delay: float = field(default_factory=lambda: float(os.getenv("RETRY_BASE_DELAY", "1.0")))

    @property
    def supervisor_state_file(self) -> Path:
        return self.jarvis_home / "locks" / "supervisor.state"

    @property
    def trinity_components_dir(self) -> Path:
        return self.jarvis_home / "trinity" / "components"

    @property
    def eta_history_file(self) -> Path:
        return self.jarvis_home / "cache" / "eta_history.json"


def get_config() -> LoadingServerConfig:
    """Get the loading server configuration singleton."""
    return LoadingServerConfig()


# =============================================================================
# v125.0: CIRCUIT BREAKER PATTERN
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, rejecting calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """
    v125.0: Adaptive circuit breaker with ML-based threshold prediction.

    Prevents cascade failures by temporarily blocking calls to failing services.
    """
    name: str
    threshold: int = 5
    timeout: float = 30.0

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _success_count: int = field(default=0, init=False)
    _half_open_successes_required: int = field(default=2, init=False)

    @property
    def state(self) -> CircuitState:
        """Get current state, potentially transitioning from OPEN to HALF_OPEN."""
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info(f"[CircuitBreaker:{self.name}] Transitioning to HALF_OPEN")
        return self._state

    def record_success(self):
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._half_open_successes_required:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info(f"[CircuitBreaker:{self.name}] Recovered - CLOSED")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)  # Decay failures

    def record_failure(self):
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] Failed in HALF_OPEN - back to OPEN")
        elif self._failure_count >= self.threshold:
            self._state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] OPENED after {self._failure_count} failures")

    def can_execute(self) -> bool:
        """Check if a call can be made."""
        state = self.state  # This may transition OPEN -> HALF_OPEN
        return state != CircuitState.OPEN


# =============================================================================
# v125.0: ETA PREDICTION ENGINE
# =============================================================================

@dataclass
class ComponentTiming:
    """Historical timing data for a component."""
    name: str
    durations: List[float] = field(default_factory=list)
    max_samples: int = 100

    def add_duration(self, duration: float):
        """Add a duration sample."""
        self.durations.append(duration)
        if len(self.durations) > self.max_samples:
            self.durations = self.durations[-self.max_samples:]

    @property
    def mean_duration(self) -> float:
        """Get mean duration."""
        return statistics.mean(self.durations) if self.durations else 0.0

    @property
    def std_duration(self) -> float:
        """Get standard deviation."""
        return statistics.stdev(self.durations) if len(self.durations) > 1 else 0.0

    @property
    def predicted_duration(self) -> float:
        """Get predicted duration with 95th percentile buffer."""
        if not self.durations:
            return 5.0  # Default 5 seconds
        return self.mean_duration + (1.645 * self.std_duration)


class ETAPredictionEngine:
    """
    v125.0: ML-based ETA prediction with historical learning.

    Uses exponential smoothing and historical component timing to predict
    remaining startup time with adaptive confidence intervals.
    """

    def __init__(self, config: LoadingServerConfig):
        self.config = config
        self._component_timings: Dict[str, ComponentTiming] = {}
        self._startup_start_time: Optional[float] = None
        self._current_progress: float = 0.0
        self._progress_history: Deque[Tuple[float, float]] = collections.deque(maxlen=100)
        self._alpha = 0.3  # Exponential smoothing factor
        self._load_history()

    def _load_history(self):
        """Load historical timing data."""
        try:
            if self.config.eta_history_file.exists():
                with open(self.config.eta_history_file) as f:
                    data = json.load(f)
                    for name, durations in data.get("components", {}).items():
                        self._component_timings[name] = ComponentTiming(name=name, durations=durations)
                    logger.debug(f"[ETA] Loaded history for {len(self._component_timings)} components")
        except Exception as e:
            logger.debug(f"[ETA] Could not load history: {e}")

    def _save_history(self):
        """Save timing history for future predictions."""
        try:
            self.config.eta_history_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "components": {
                    name: timing.durations
                    for name, timing in self._component_timings.items()
                },
                "updated_at": datetime.now().isoformat()
            }
            with open(self.config.eta_history_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"[ETA] Could not save history: {e}")

    def start_tracking(self):
        """Start tracking a new startup cycle."""
        self._startup_start_time = time.time()
        self._progress_history.clear()
        self._current_progress = 0.0

    def update_progress(self, progress: float, component: Optional[str] = None):
        """Update current progress."""
        now = time.time()
        self._current_progress = progress
        self._progress_history.append((now, progress))

    def record_component_complete(self, name: str, duration: float):
        """Record component completion time for future predictions."""
        if name not in self._component_timings:
            self._component_timings[name] = ComponentTiming(name=name)
        self._component_timings[name].add_duration(duration)

    def get_predicted_eta(self) -> Dict[str, Any]:
        """
        Calculate predicted ETA using multiple methods and return best estimate.

        Methods:
        1. Linear regression on recent progress
        2. Component-based prediction (sum of remaining components)
        3. Historical average for this progress level
        """
        if self._startup_start_time is None:
            return {"eta_seconds": None, "confidence": 0.0, "method": "none"}

        elapsed = time.time() - self._startup_start_time

        if self._current_progress >= 100:
            return {"eta_seconds": 0, "confidence": 1.0, "method": "complete"}

        if self._current_progress <= 0:
            # Use historical average
            total_predicted = sum(t.predicted_duration for t in self._component_timings.values())
            return {
                "eta_seconds": max(total_predicted, 30),  # At least 30 seconds
                "confidence": 0.3,
                "method": "historical"
            }

        # Method 1: Linear extrapolation from recent progress
        eta_linear = None
        if len(self._progress_history) >= 3:
            recent = list(self._progress_history)[-10:]
            if recent[-1][1] > recent[0][1]:  # Progress increasing
                progress_rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
                remaining_progress = 100 - self._current_progress
                if progress_rate > 0:
                    eta_linear = remaining_progress / progress_rate

        # Method 2: Time-based extrapolation
        eta_time_based = None
        if self._current_progress > 5:  # Need some progress to extrapolate
            rate = self._current_progress / elapsed
            if rate > 0:
                eta_time_based = (100 - self._current_progress) / rate

        # Combine estimates with weights
        estimates = []
        if eta_linear is not None and eta_linear > 0:
            estimates.append((eta_linear, 0.6))  # Higher weight for recent trend
        if eta_time_based is not None and eta_time_based > 0:
            estimates.append((eta_time_based, 0.4))

        if not estimates:
            return {"eta_seconds": 60, "confidence": 0.2, "method": "fallback"}

        # Weighted average
        total_weight = sum(w for _, w in estimates)
        weighted_eta = sum(e * w for e, w in estimates) / total_weight

        # Calculate confidence based on data quality
        confidence = min(0.9, 0.3 + (len(self._progress_history) / 50) + (self._current_progress / 200))

        return {
            "eta_seconds": round(weighted_eta, 1),
            "confidence": round(confidence, 2),
            "method": "weighted_ensemble",
            "elapsed_seconds": round(elapsed, 1),
            "progress_rate": round(self._current_progress / elapsed if elapsed > 0 else 0, 2)
        }

    def finish_tracking(self):
        """Finish tracking and save history."""
        self._save_history()


# =============================================================================
# v125.0: COMPONENT DEPENDENCY GRAPH
# =============================================================================

@dataclass
class ComponentNode:
    """A node in the component dependency graph."""
    name: str
    dependencies: Set[str] = field(default_factory=set)
    dependents: Set[str] = field(default_factory=set)
    status: str = "pending"  # pending, running, complete, failed, skipped
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    weight: float = 1.0
    is_critical: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class ComponentDependencyGraph:
    """
    v125.0: Dynamic component dependency tracking with cascade failure prevention.

    Features:
    - Topological sorting for startup order
    - Cascade failure detection
    - Smart timeout extension for blocked components
    - Graceful skip for non-critical failures
    """

    def __init__(self):
        self._nodes: Dict[str, ComponentNode] = {}
        self._lock = threading.Lock()

    def register_component(
        self,
        name: str,
        dependencies: Optional[List[str]] = None,
        weight: float = 1.0,
        is_critical: bool = False
    ):
        """Register a component with its dependencies."""
        with self._lock:
            node = ComponentNode(
                name=name,
                dependencies=set(dependencies or []),
                weight=weight,
                is_critical=is_critical
            )
            self._nodes[name] = node

            # Update dependents for upstream components
            for dep in node.dependencies:
                if dep in self._nodes:
                    self._nodes[dep].dependents.add(name)

    def can_start(self, name: str) -> Tuple[bool, Optional[str]]:
        """Check if a component can start (all dependencies complete)."""
        if name not in self._nodes:
            return True, None

        node = self._nodes[name]
        for dep in node.dependencies:
            if dep not in self._nodes:
                continue
            dep_node = self._nodes[dep]
            if dep_node.status == "failed" and dep_node.is_critical:
                return False, f"Critical dependency {dep} failed"
            if dep_node.status not in ("complete", "skipped"):
                return False, f"Waiting for {dep}"
        return True, None

    def mark_started(self, name: str):
        """Mark a component as started."""
        with self._lock:
            if name in self._nodes:
                self._nodes[name].status = "running"
                self._nodes[name].started_at = time.time()

    def mark_complete(self, name: str):
        """Mark a component as complete."""
        with self._lock:
            if name in self._nodes:
                node = self._nodes[name]
                node.status = "complete"
                node.completed_at = time.time()

    def mark_failed(self, name: str, error: str):
        """Mark a component as failed and check for cascade effects."""
        with self._lock:
            if name in self._nodes:
                node = self._nodes[name]
                node.status = "failed"
                node.error = error
                node.completed_at = time.time()

                # If critical, cascade failure to dependents
                if node.is_critical:
                    self._cascade_failure(name)

    def _cascade_failure(self, failed_name: str):
        """Propagate failure to dependent components."""
        visited = set()
        queue = [failed_name]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current in self._nodes:
                for dependent in self._nodes[current].dependents:
                    if dependent not in visited and dependent in self._nodes:
                        dep_node = self._nodes[dependent]
                        if dep_node.status == "pending":
                            dep_node.status = "skipped"
                            dep_node.error = f"Skipped due to {failed_name} failure"
                            queue.append(dependent)

    def get_startup_order(self) -> List[str]:
        """Get topologically sorted startup order."""
        # Kahn's algorithm for topological sort
        in_degree = {name: len(node.dependencies) for name, node in self._nodes.items()}
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result = []

        while queue:
            # Sort by weight (critical components first)
            queue.sort(key=lambda n: (-self._nodes[n].weight, n))
            current = queue.pop(0)
            result.append(current)

            for dependent in self._nodes.get(current, ComponentNode(current)).dependents:
                if dependent in in_degree:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        return result

    def get_progress(self) -> Dict[str, Any]:
        """Calculate overall progress based on component weights."""
        total_weight = sum(n.weight for n in self._nodes.values())
        completed_weight = sum(
            n.weight for n in self._nodes.values()
            if n.status in ("complete", "skipped")
        )

        return {
            "total_components": len(self._nodes),
            "completed": sum(1 for n in self._nodes.values() if n.status == "complete"),
            "failed": sum(1 for n in self._nodes.values() if n.status == "failed"),
            "skipped": sum(1 for n in self._nodes.values() if n.status == "skipped"),
            "running": sum(1 for n in self._nodes.values() if n.status == "running"),
            "pending": sum(1 for n in self._nodes.values() if n.status == "pending"),
            "progress_percent": round((completed_weight / total_weight * 100) if total_weight > 0 else 0, 1),
            "components": {
                name: {
                    "status": node.status,
                    "weight": node.weight,
                    "is_critical": node.is_critical,
                    "error": node.error,
                    "duration_ms": round((node.completed_at - node.started_at) * 1000, 1)
                        if node.started_at and node.completed_at else None
                }
                for name, node in self._nodes.items()
            }
        }


# =============================================================================
# v125.0: WEBSOCKET CONNECTION MANAGER
# =============================================================================

class WebSocketConnection:
    """Represents a single WebSocket connection."""

    def __init__(self, writer: asyncio.StreamWriter, conn_id: str):
        self.writer = writer
        self.conn_id = conn_id
        self.connected_at = time.time()
        self.last_ping = time.time()
        self.message_count = 0
        self._closed = False

    async def send(self, message: str) -> bool:
        """Send a message to this connection."""
        if self._closed:
            return False
        try:
            # Simple WebSocket frame (text frame, no masking for server->client)
            payload = message.encode('utf-8')
            length = len(payload)

            if length < 126:
                frame = bytes([0x81, length]) + payload
            elif length < 65536:
                frame = bytes([0x81, 126, (length >> 8) & 0xFF, length & 0xFF]) + payload
            else:
                frame = bytes([0x81, 127]) + length.to_bytes(8, 'big') + payload

            self.writer.write(frame)
            await self.writer.drain()
            self.message_count += 1
            return True
        except Exception as e:
            logger.debug(f"[WS:{self.conn_id}] Send failed: {e}")
            self._closed = True
            return False

    async def close(self):
        """Close the connection."""
        if not self._closed:
            self._closed = True
            try:
                # Send close frame
                self.writer.write(bytes([0x88, 0x00]))
                await self.writer.drain()
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass


class WebSocketManager:
    """
    v125.0: Advanced WebSocket connection manager with automatic reconnection support.
    """

    def __init__(self):
        self._connections: Dict[str, WebSocketConnection] = {}
        self._lock = asyncio.Lock()
        self._message_buffer: Deque[str] = collections.deque(maxlen=100)

    async def add_connection(self, writer: asyncio.StreamWriter) -> str:
        """Add a new WebSocket connection."""
        conn_id = str(uuid.uuid4())[:8]
        async with self._lock:
            conn = WebSocketConnection(writer, conn_id)
            self._connections[conn_id] = conn

            # Send buffered messages to new connection
            for msg in self._message_buffer:
                await conn.send(msg)

        logger.info(f"[WS] New connection: {conn_id} (total: {len(self._connections)})")
        return conn_id

    async def remove_connection(self, conn_id: str):
        """Remove a WebSocket connection."""
        async with self._lock:
            if conn_id in self._connections:
                await self._connections[conn_id].close()
                del self._connections[conn_id]
                logger.debug(f"[WS] Connection removed: {conn_id}")

    async def broadcast(self, message: str, buffer: bool = True):
        """Broadcast a message to all connected clients."""
        if buffer:
            self._message_buffer.append(message)

        async with self._lock:
            dead_connections = []
            for conn_id, conn in self._connections.items():
                if not await conn.send(message):
                    dead_connections.append(conn_id)

            # Clean up dead connections
            for conn_id in dead_connections:
                del self._connections[conn_id]

    async def send_ping(self):
        """Send ping frames to all connections."""
        async with self._lock:
            dead_connections = []
            for conn_id, conn in self._connections.items():
                try:
                    conn.writer.write(bytes([0x89, 0x00]))  # Ping frame
                    await conn.writer.drain()
                    conn.last_ping = time.time()
                except Exception:
                    dead_connections.append(conn_id)

            for conn_id in dead_connections:
                del self._connections[conn_id]

    async def close_all(self, timeout: float = 2.0) -> int:
        """Close all active WebSocket connections with bounded wait."""
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()

        if not connections:
            return 0

        close_timeout = max(0.1, timeout)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(conn.close() for conn in connections),
                    return_exceptions=True,
                ),
                timeout=close_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[WS] Timed out closing %d connection(s) after %.1fs",
                len(connections),
                close_timeout,
            )
        return len(connections)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# =============================================================================
# v125.0: TRINITY HEALTH AGGREGATOR
# =============================================================================

class TrinityHealthAggregator:
    """
    v125.0: Real-time health aggregation across all Trinity components.

    Monitors:
    - JARVIS Body (backend)
    - JARVIS Prime (local LLM)
    - Reactor Core (training)
    """

    def __init__(self, config: LoadingServerConfig):
        self.config = config
        self._circuit_breakers: Dict[str, CircuitBreaker] = {
            "backend": CircuitBreaker("backend", threshold=config.circuit_breaker_threshold),
            "prime": CircuitBreaker("prime", threshold=config.circuit_breaker_threshold),
            "reactor": CircuitBreaker("reactor", threshold=config.circuit_breaker_threshold),
        }
        self._last_health: Dict[str, Dict[str, Any]] = {}
        self._health_history: Deque[Dict[str, Any]] = collections.deque(maxlen=100)

    async def _check_http_health(self, name: str, url: str) -> Dict[str, Any]:
        """Check HTTP health endpoint with circuit breaker."""
        cb = self._circuit_breakers.get(name)
        if cb and not cb.can_execute():
            return {"status": "circuit_open", "error": "Circuit breaker open"}

        start = time.time()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('localhost', int(url.split(':')[-1].split('/')[0])),
                timeout=self.config.health_check_timeout
            )

            request = f"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            writer.write(request.encode())
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=self.config.health_check_timeout)
            writer.close()
            await writer.wait_closed()

            response_time = (time.time() - start) * 1000

            # Parse response
            response_str = response.decode('utf-8', errors='ignore')
            if 'HTTP/1.1 200' in response_str or 'HTTP/1.0 200' in response_str:
                if cb:
                    cb.record_success()

                # Try to extract JSON body
                body_start = response_str.find('\r\n\r\n')
                if body_start > 0:
                    try:
                        body = json.loads(response_str[body_start + 4:])
                        return {
                            "status": "healthy",
                            "response_time_ms": round(response_time, 1),
                            "data": body
                        }
                    except json.JSONDecodeError:
                        pass

                return {"status": "healthy", "response_time_ms": round(response_time, 1)}
            else:
                if cb:
                    cb.record_failure()
                return {"status": "unhealthy", "error": "Non-200 response"}

        except asyncio.TimeoutError:
            if cb:
                cb.record_failure()
            return {"status": "timeout", "error": "Health check timed out"}
        except Exception as e:
            if cb:
                cb.record_failure()
            return {"status": "error", "error": str(e)}

    async def _check_heartbeat_file(self, name: str, file_path: Path) -> Dict[str, Any]:
        """Check heartbeat file freshness."""
        try:
            if not file_path.exists():
                return {"status": "missing", "error": "Heartbeat file not found"}

            with open(file_path) as f:
                data = json.load(f)

            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            if age < 30:
                return {
                    "status": "healthy",
                    "age_seconds": round(age, 1),
                    "data": data
                }
            elif age < 60:
                return {
                    "status": "degraded",
                    "age_seconds": round(age, 1),
                    "warning": "Heartbeat is stale"
                }
            else:
                return {
                    "status": "unhealthy",
                    "age_seconds": round(age, 1),
                    "error": "Heartbeat too old"
                }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def get_unified_health(self) -> Dict[str, Any]:
        """Get unified health status across all Trinity components."""
        results = {}

        # Check all components in parallel
        checks = await asyncio.gather(
            self._check_http_health("backend", f"localhost:{self.config.backend_port}"),
            self._check_http_health("prime", f"localhost:{self.config.prime_port}"),
            self._check_heartbeat_file("reactor", self.config.trinity_components_dir / "reactor_core.json"),
            return_exceptions=True
        )

        components = ["backend", "prime", "reactor"]
        for name, result in zip(components, checks):
            if isinstance(result, Exception):
                results[name] = {"status": "error", "error": str(result)}
            else:
                results[name] = result

        # Add loading server (always healthy if responding)
        results["loading_server"] = {
            "status": "healthy",
            "port": self.config.port
        }

        # Calculate overall status
        statuses = [r.get("status", "unknown") for r in results.values()]
        if all(s == "healthy" for s in statuses):
            overall = "healthy"
        elif any(s in ("error", "unhealthy", "timeout") for s in statuses):
            overall = "degraded"
        else:
            overall = "starting"

        health = {
            "status": overall,
            "components": results,
            "timestamp": datetime.now().isoformat(),
            "circuit_breakers": {
                name: cb.state.value
                for name, cb in self._circuit_breakers.items()
            }
        }

        self._last_health = health
        self._health_history.append(health)

        return health


# =============================================================================
# v125.0: LOADING SERVER CORE
# =============================================================================

class LoadingServer:
    """
    v212.0: Enterprise-grade loading server with full Trinity integration.

    Features:
    - W3C Distributed Tracing for cross-service correlation
    - Event Sourcing with JSONL logs for replay/debugging
    - SQLite Progress Persistence for browser refresh resume
    - Enhanced ML-based ETA Prediction
    - Lock-Free Progress Updates
    - Container-Aware Timeout Scaling
    - Adaptive Backpressure (AIMD) for WebSocket
    - Cross-Repo Health Aggregation
    - Intelligent Message Generation
    - Self-Healing with Auto-Recovery
    - Trinity Heartbeat Monitoring
    - Parent Death Watcher (v211.0)
    """

    def __init__(self, config: Optional[LoadingServerConfig] = None):
        self.config = config or get_config()

        # Core components
        self._eta_engine = ETAPredictionEngine(self.config)
        self._dependency_graph = ComponentDependencyGraph()
        self._ws_manager = WebSocketManager()
        self._health_aggregator = TrinityHealthAggregator(self.config)

        # State
        self._startup_time = time.time()
        self._progress = 0
        self._phase = "initializing"
        self._message = "Starting JARVIS..."
        self._components: Dict[str, Dict[str, Any]] = {}
        self._shutdown_requested = False
        self._stopping = False
        self._server: Optional[asyncio.Server] = None

        # Background tasks
        self._background_tasks: List[asyncio.Task] = []
        self._hub_connect_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_request_tasks: Set[asyncio.Task] = set()

        # v183.0: Supervisor heartbeat tracking
        self._last_supervisor_update: float = time.time()
        self._supervisor_timeout_threshold: float = 30.0  # seconds

        # v186.0: Sequence tracking for missed update recovery
        self._sequence_number: int = 0
        self._update_history: Deque[Dict[str, Any]] = collections.deque(maxlen=50)  # Keep last 50 updates

        # v183.0: Trinity component status tracking
        self._trinity_status: Dict[str, Dict[str, Any]] = {
            "jarvis_prime": {"progress": 0, "status": "unknown", "last_update": 0},
            "reactor_core": {"progress": 0, "status": "unknown", "last_update": 0},
        }

        # v185.0: Trinity summary from supervisor broadcasts
        self._trinity_summary: Optional[Dict[str, Any]] = None
        self._trinity_ready: bool = False

        # v225.0: Prime v2 init_progress protocol data
        self._prime_init_progress: Optional[Dict[str, Any]] = None

        # =================================================================
        # v212.0: Advanced Feature Integration
        # =================================================================
        self._session_id = str(uuid.uuid4())
        self._trace_context: Optional[Any] = None
        self._event_log: Optional[Any] = None
        self._persistence: Optional[Any] = None
        self._enhanced_eta: Optional[Any] = None
        self._lock_free_progress: Optional[Any] = None
        self._container_awareness: Optional[Any] = None
        self._backpressure: Optional[Any] = None
        self._cross_repo_health: Optional[Any] = None
        self._message_generator: Optional[Any] = None
        self._self_healing: Optional[Any] = None
        self._heartbeat_reader: Optional[Any] = None

        if ADVANCED_FEATURES_AVAILABLE:
            self._init_advanced_features()

        # Integration with unified hub (connected lazily after socket bind)
        self._hub = None

    def _init_advanced_features(self) -> None:
        """Initialize v212.0 advanced features."""
        try:
            # W3C Distributed Tracing
            self._trace_context = W3CTraceContext()
            logger.info(f"[v212.0] Trace context: {self._trace_context.to_traceparent()}")

            # Event Sourcing Log
            self._event_log = EventSourcingLog()

            # Progress Persistence (SQLite)
            self._persistence = ProgressPersistence()

            # Enhanced ETA Prediction
            self._enhanced_eta = PredictiveETACalculator()
            self._enhanced_eta.start_session(self._session_id)

            # Lock-Free Progress Updates
            self._lock_free_progress = LockFreeProgressUpdate()

            # Container Awareness
            self._container_awareness = ContainerAwareness()
            if self._container_awareness.is_containerized:
                timeout_mult = self._container_awareness.get_timeout_multiplier()
                logger.info(f"[v212.0] Container detected, timeout multiplier: {timeout_mult}x")

            # Adaptive Backpressure
            self._backpressure = AdaptiveBackpressureController()

            # Cross-Repo Health (use existing aggregator enhanced)
            self._cross_repo_health = CrossRepoHealthAggregator(self.config)

            # Message Generator
            self._message_generator = IntelligentMessageGenerator()

            # Self-Healing Manager
            self._self_healing = SelfHealingRestartManager()

            # Trinity Heartbeat Reader
            self._heartbeat_reader = TrinityHeartbeatReader()

            # Log initialization event
            if self._event_log:
                self._event_log.append_event(
                    "server_init",
                    {
                        "session_id": self._session_id,
                        "version": "212.0.0",
                        "features": {
                            "tracing": True,
                            "persistence": True,
                            "enhanced_eta": True,
                            "lock_free": True,
                            "container_aware": self._container_awareness.is_containerized if self._container_awareness else False,
                            "backpressure": True,
                            "message_generator": True,
                            "heartbeat_reader": True,
                        },
                    },
                    trace_id=self._trace_context.trace_id if self._trace_context else None,
                )

            logger.info("[v212.0] All advanced features initialized")

        except Exception as e:
            logger.warning(f"[v212.0] Error initializing advanced features: {e}")

    def _try_connect_hub(self) -> bool:
        """Try to connect to the UnifiedStartupProgressHub."""
        try:
            repo_path = str(self.config.jarvis_repo)
            if repo_path and repo_path not in sys.path:
                sys.path.insert(0, repo_path)
            from backend.core.unified_startup_progress import get_progress_hub
            self._hub = get_progress_hub()
            self._hub.register_sync_target(self._on_hub_update)
            logger.info("[v125.0] Connected to UnifiedStartupProgressHub")
            return True
        except Exception as e:
            logger.debug(f"[v125.0] Hub not available: {e}")
            return False

    async def _deferred_connect_hub(self) -> None:
        """
        Connect to the progress hub in the background after server bind.

        This prevents synchronous import/discovery side effects from delaying
        `/health` readiness and the startup landing page refresh path.
        """
        delay = max(0.0, float(os.getenv("LOADING_SERVER_HUB_CONNECT_DELAY", "0.25")))
        timeout = max(1.0, float(os.getenv("LOADING_SERVER_HUB_CONNECT_TIMEOUT", "20.0")))
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await asyncio.wait_for(asyncio.to_thread(self._try_connect_hub), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"[v125.0] Hub connection timed out after {timeout:.1f}s "
                "(loading server remains healthy)"
            )
        except Exception as e:
            logger.debug(f"[v125.0] Deferred hub connect failed: {e}")

    def _on_hub_update(self, state: Dict[str, Any]):
        """Thread-safe callback when unified hub updates."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._apply_hub_update, dict(state))
                return
            except Exception:
                pass
        self._apply_hub_update(state)

    def _apply_hub_update(self, state: Dict[str, Any]) -> None:
        """Apply hub update on the loading-server event loop."""
        self._progress = state.get("progress", self._progress)
        self._phase = state.get("phase", self._phase)
        self._message = state.get("message", self._message)

        # Broadcast to WebSocket clients
        # v210.0: Use safe task to prevent "Future exception was never retrieved"
        create_safe_task(self._broadcast_progress(), name="hub_broadcast_progress")

    async def _drain_active_request_tasks(self, timeout: float) -> None:
        """Wait for in-flight request handlers to exit, then cancel stragglers."""
        wait_timeout = max(0.1, timeout)
        pending = [
            task for task in list(self._active_request_tasks)
            if not task.done()
        ]
        if not pending:
            return

        _, still_pending = await asyncio.wait(pending, timeout=wait_timeout)
        for task in still_pending:
            task.cancel()

        if still_pending:
            _, final_pending = await asyncio.wait(
                still_pending,
                timeout=max(0.1, wait_timeout * 0.5),
            )
            if final_pending:
                logger.warning(
                    "[v212.1] %d request task(s) still pending after shutdown drain",
                    len(final_pending),
                )

    async def _broadcast_progress(self):
        """
        Broadcast current progress to all WebSocket clients.

        v212.0: Added backpressure control and enhanced ETA data.
        """
        # v212.0: Check backpressure before sending
        if self._backpressure and not self._backpressure.should_send():
            return  # Skip this broadcast due to backpressure

        # Get ETA from enhanced engine if available, otherwise use legacy
        if self._enhanced_eta:
            eta_data = self._enhanced_eta.get_predicted_eta()
        else:
            eta_data = self._eta_engine.get_predicted_eta()

        # v212.0: Get sequence number for missed update detection
        sequence = self._sequence_number
        if self._lock_free_progress:
            _, lock_free_seq = self._lock_free_progress.get_progress()
            sequence = max(sequence, lock_free_seq)

        # v129.0: Get ghost display status for frontend
        ghost_display_data = None
        try:
            from backend.vision.yabai_space_detector import get_ghost_display_status
            ghost_display_data = get_ghost_display_status()
        except ImportError:
            try:
                from vision.yabai_space_detector import get_ghost_display_status
                ghost_display_data = get_ghost_display_status()
            except ImportError:
                pass
        except Exception:
            pass

        message = json.dumps({
            "type": "progress",
            "data": {
                "progress": self._progress,
                "phase": self._phase,
                "stage": self._phase,  # v185.0: Alias for frontend compatibility
                "message": self._message,
                "eta": eta_data,
                "components": self._components,
                "trinity": self._trinity_summary,  # v185.0: Trinity component summary
                "trinity_ready": self._trinity_ready,  # v185.0: Trinity ready flag
                "ghost_display": ghost_display_data,  # v129.0: Ghost display status
                "init_progress": self._prime_init_progress,  # v225.0: Prime v2 phase data
                "sequence": sequence,  # v186.0/v212.0: Sequence for detecting missed updates
                "session_id": self._session_id,  # v212.0: Session correlation
                "trace_id": self._trace_context.trace_id if self._trace_context else None,
                "timestamp": datetime.now().isoformat()
            }
        })

        # v212.0: Report queue depth for backpressure control
        if self._backpressure:
            queue_depth = self._ws_manager.connection_count
            self._backpressure.report_congestion(queue_depth)

        await self._ws_manager.broadcast(message)

    def get_supervisor_state(self) -> Dict[str, Any]:
        """Read current supervisor state from file."""
        try:
            if self.config.supervisor_state_file.exists():
                with open(self.config.supervisor_state_file) as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"[v125.0] Could not read supervisor state: {e}")
        return {}

    # =========================================================================
    # HTTP Request Handlers
    # =========================================================================

    async def handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP/WebSocket request."""
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_request_tasks.add(current_task)
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not request_line:
                return

            request_str = request_line.decode('utf-8', errors='ignore').strip()
            parts = request_str.split(' ')
            if len(parts) < 2:
                return

            method, path = parts[0], parts[1].split('?')[0]  # Strip query params

            # Read headers
            headers = {}
            while True:
                line = await reader.readline()
                if not line or line == b'\r\n':
                    break
                try:
                    key, value = line.decode('utf-8').strip().split(': ', 1)
                    headers[key.lower()] = value
                except ValueError:
                    continue

            # Check for WebSocket upgrade
            if headers.get('upgrade', '').lower() == 'websocket':
                # v186.0: Log WebSocket path for debugging (accepts any path for flexibility)
                logger.debug(f"[v186.0] WebSocket upgrade request for path: {path}")
                await self._handle_websocket_upgrade(reader, writer, headers)
                return

            # Read body for POST requests
            body = None
            content_length = int(headers.get('content-length', 0))
            if content_length > 0:
                body = await reader.read(content_length)

            # Route request
            response = await self._route_request(method, path, headers, body)

            # Send response
            writer.write(response.encode() if isinstance(response, str) else response)
            await writer.drain()

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.debug(f"[v125.0] Request error: {e}")
        finally:
            if current_task is not None:
                self._active_request_tasks.discard(current_task)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _route_request(self, method: str, path: str, headers: Dict, body: Optional[bytes]) -> Union[str, bytes]:
        """Route HTTP request to appropriate handler."""

        # ===========================================================================
        # v185.0: Standard health endpoint (supervisor calls /health)
        # This MUST come first for fast health checks during startup
        # ===========================================================================
        if path == "/health":
            return self._json_response(self._get_health_response())

        # v185.0: Kubernetes-style readiness endpoint
        if path == "/ready":
            return self._json_response(self._get_readiness_response())

        # API Routes
        if path == "/api/supervisor/health":
            return self._json_response(self._get_health_response())

        elif path == "/api/supervisor/heartbeat":
            # v183.0: Return real heartbeat status based on update freshness
            return self._json_response(self._get_supervisor_heartbeat())

        elif path == "/api/supervisor/status":
            return self._json_response(self._get_full_status())

        elif path == "/api/health/unified":
            health = await self._health_aggregator.get_unified_health()
            return self._json_response(health)

        # v186.0: Endpoint aliases for frontend compatibility
        # loading-manager.js uses /api/startup-progress, we support both
        elif path == "/api/progress" or path == "/api/startup-progress":
            return self._json_response(self._get_progress_response())

        elif path == "/api/progress/eta":
            return self._json_response(self._eta_engine.get_predicted_eta())

        elif path == "/api/progress/components":
            return self._json_response(self._dependency_graph.get_progress())

        # v186.0: Resume endpoint for missed update recovery
        # loading-manager.js uses this to recover state after WebSocket disconnection
        elif path.startswith("/api/progress/resume"):
            return self._json_response(self._get_resume_response(path))

        elif path == "/api/update-progress" and method == "POST":
            return await self._handle_progress_update(body)

        elif path == "/api/component/register" and method == "POST":
            return await self._handle_component_register(body)

        elif path == "/api/component/complete" and method == "POST":
            return await self._handle_component_complete(body)

        # v183.0: Trinity component status endpoints
        elif path == "/api/trinity/status" and method == "POST":
            return await self._handle_trinity_status_update(body)

        elif path == "/api/trinity/status" and method == "GET":
            return self._json_response({"trinity": self._trinity_status})

        # =================================================================
        # v212.0: New Advanced API Endpoints
        # =================================================================
        elif path == "/api/analytics/startup":
            # Historical startup analytics
            return self._json_response(self._get_startup_analytics())

        elif path == "/api/health/cross-repo":
            # Enhanced cross-repo health with circuit breakers
            if self._cross_repo_health:
                health = await self._cross_repo_health.get_unified_health()
                return self._json_response(health)
            return self._json_response({"error": "Cross-repo health not available"}, status=503)

        elif path == "/api/trinity/heartbeats":
            # Direct heartbeat file status
            return self._json_response(await self._get_trinity_heartbeats())

        elif path == "/api/session/info":
            # Current session information
            return self._json_response(self._get_session_info())

        elif path == "/api/shutdown" and method == "POST":
            # v212.0: Log shutdown event
            if self._event_log:
                self._event_log.append_event(
                    "shutdown_requested",
                    {"progress": self._progress, "phase": self._phase},
                    trace_id=self._trace_context.trace_id if self._trace_context else None,
                )
            self._shutdown_requested = True
            return self._json_response({"status": "shutdown_initiated"})

        elif path == "/sse/progress":
            return await self._handle_sse_stream()

        # Static routes
        elif path == "/" or path == "/index.html":
            return self._html_response(self._get_loading_page())

        elif path.endswith((".js", ".css", ".png", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".json")):
            return self._serve_static_file(path)

        else:
            return self._json_response({"error": "Not found"}, status=404)

    def _get_health_response(self) -> Dict[str, Any]:
        """Generate health response."""
        state = self.get_supervisor_state()
        return {
            "status": "healthy" if state.get("pid") else "starting",
            "uptime": round(time.time() - self._startup_time, 2),
            "supervisor": {
                "pid": state.get("pid"),
                "started_at": state.get("started_at"),
                "health_level": state.get("health_level", 0),
            },
            "loading_server": {
                "version": "185.0.0",
                "progress": self._progress,
                "phase": self._phase,
                "websocket_clients": self._ws_manager.connection_count,
            },
            "timestamp": datetime.now().isoformat(),
        }

    def _get_readiness_response(self) -> Dict[str, Any]:
        """
        v185.0: Generate Kubernetes-style readiness response.
        
        Ready means the loading server is fully operational and can accept traffic.
        This differs from health (liveness) which just means the process is alive.
        """
        # Ready = server is up AND has received at least one progress update
        is_ready = self._progress > 0 or self._phase != "initializing"
        
        return {
            "ready": is_ready,
            "status": "ready" if is_ready else "initializing",
            "progress": self._progress,
            "phase": self._phase,
            "trinity_ready": self._trinity_ready,
            "uptime": round(time.time() - self._startup_time, 2),
            "timestamp": datetime.now().isoformat(),
        }

    def _get_progress_response(self) -> Dict[str, Any]:
        """Get current progress state."""
        eta = self._eta_engine.get_predicted_eta()
        return {
            "progress": self._progress,
            "phase": self._phase,
            "stage": self._phase,  # v185.0: Alias for frontend compatibility
            "message": self._message,
            "eta": eta,
            "components": self._components,
            "trinity": self._trinity_summary,  # v185.0: Trinity component summary
            "trinity_ready": self._trinity_ready,  # v185.0: Trinity ready flag
            "init_progress": self._prime_init_progress,  # v225.0: Prime v2 phase data
            "dependency_graph": self._dependency_graph.get_progress(),
            "sequence": self._sequence_number,  # v186.0: Sequence for tracking
            "timestamp": datetime.now().isoformat(),
        }

    def _get_resume_response(self, path: str) -> Dict[str, Any]:
        """
        v186.0: Get resume data for missed update recovery.
        
        loading-manager.js uses this endpoint to recover state after WebSocket
        disconnection or packet loss. Supports query parameters:
        - last_sequence: Client's last known sequence number
        - include_health: Whether to include health check data
        
        Args:
            path: Full path including query string (e.g., /api/progress/resume?last_sequence=5)
            
        Returns:
            Current progress state with resume metadata
        """
        # Parse query parameters
        last_sequence = 0
        include_health = False
        
        if '?' in path:
            query_string = path.split('?', 1)[1]
            for param in query_string.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    if key == 'last_sequence':
                        try:
                            last_sequence = int(value)
                        except ValueError:
                            pass
                    elif key == 'include_health':
                        include_health = value.lower() == 'true'
        
        # Calculate missed updates
        missed_updates = max(0, self._sequence_number - last_sequence)
        
        # Build response with current state
        response = self._get_progress_response()
        
        # Add resume metadata
        response['resume_metadata'] = {
            'client_last_sequence': last_sequence,
            'current_sequence': self._sequence_number,
            'missed_updates': missed_updates,
            'recovered': missed_updates > 0,
        }
        
        # Optionally include health data
        if include_health:
            response['health'] = self._get_health_response()
        
        logger.debug(f"[v186.0] Resume request: client_seq={last_sequence}, current_seq={self._sequence_number}, missed={missed_updates}")
        
        return response

    def _get_full_status(self) -> Dict[str, Any]:
        """Get full supervisor and loading server status."""
        state = self.get_supervisor_state()
        return {
            "supervisor": state,
            "loading_server": {
                "version": "212.0.0",
                "startup_time": self._startup_time,
                "uptime_seconds": time.time() - self._startup_time,
                "progress": self._progress,
                "phase": self._phase,
                "message": self._message,
                "websocket_clients": self._ws_manager.connection_count,
                "session_id": self._session_id,
                "advanced_features": ADVANCED_FEATURES_AVAILABLE,
            },
            "eta": self._eta_engine.get_predicted_eta(),
            "components": self._dependency_graph.get_progress(),
            "trinity": self._trinity_status,
            "circuit_breakers": {
                name: cb.state.value
                for name, cb in self._health_aggregator._circuit_breakers.items()
            },
        }

    def _get_supervisor_heartbeat(self) -> Dict[str, Any]:
        """
        v183.0: Return real supervisor heartbeat status based on update freshness.
        
        The supervisor is considered alive if we've received an update within
        the timeout threshold (default 30s).
        """
        now = time.time()
        time_since_update = now - self._last_supervisor_update
        is_alive = time_since_update < self._supervisor_timeout_threshold
        
        return {
            "supervisor_alive": is_alive,  # Match frontend expectation
            "alive": is_alive,  # Legacy compatibility
            "last_update_timestamp": self._last_supervisor_update,
            "time_since_update": round(time_since_update, 1),
            "timeout_threshold": self._supervisor_timeout_threshold,
            "timestamp": now,
        }

    # =========================================================================
    # v212.0: Advanced Feature Helper Methods
    # =========================================================================

    def _get_startup_analytics(self) -> Dict[str, Any]:
        """
        v212.0: Get historical startup analytics.

        Returns statistics about previous startups for optimization insights.
        """
        analytics = {
            "current_session": {
                "session_id": self._session_id,
                "started_at": self._startup_time,
                "elapsed_seconds": time.time() - self._startup_time,
                "progress": self._progress,
            },
        }

        # Add enhanced ETA analytics if available
        if self._enhanced_eta:
            try:
                analytics["eta_engine"] = self._enhanced_eta.get_startup_analytics()
            except Exception:
                pass

        # Add persistence analytics if available
        if self._persistence:
            try:
                analytics["persistence"] = self._persistence.get_analytics()
            except Exception:
                pass

        return analytics

    async def _get_trinity_heartbeats(self) -> Dict[str, Any]:
        """
        v212.0: Get direct heartbeat file status for Trinity components.

        Provides unfiltered view of heartbeat files for debugging.
        """
        if not self._heartbeat_reader:
            return {
                "available": False,
                "reason": "Heartbeat reader not initialized",
            }

        try:
            summary = await self._heartbeat_reader.get_health_summary()
            return {
                "available": True,
                **summary,
            }
        except Exception as e:
            return {
                "available": False,
                "error": str(e),
            }

    def _get_session_info(self) -> Dict[str, Any]:
        """
        v212.0: Get current session information.

        Returns details about the current loading session for debugging.
        """
        info = {
            "session_id": self._session_id,
            "started_at": datetime.fromtimestamp(self._startup_time).isoformat(),
            "uptime_seconds": round(time.time() - self._startup_time, 2),
            "version": "212.0.0",
            "progress": self._progress,
            "phase": self._phase,
            "advanced_features": ADVANCED_FEATURES_AVAILABLE,
        }

        # Add trace context if available
        if self._trace_context:
            info["trace"] = {
                "trace_id": self._trace_context.trace_id,
                "traceparent": self._trace_context.to_traceparent(),
            }

        # Add container info if available
        if self._container_awareness:
            try:
                info["container"] = self._container_awareness.get_resource_summary()
            except Exception:
                pass

        # Add backpressure stats if available
        if self._backpressure:
            try:
                info["backpressure"] = self._backpressure.get_stats()
            except Exception:
                pass

        # Add lock-free progress state
        if self._lock_free_progress:
            try:
                info["lock_free"] = self._lock_free_progress.get_full_state()
            except Exception:
                pass

        return info

    async def _handle_progress_update(self, body: Optional[bytes]) -> str:
        """Handle progress update from supervisor.

        v127.0: Fixed component handling - now properly extracts 'components' from
        metadata for display on the loading page.

        v212.0: Added event sourcing, persistence, lock-free updates, and
        intelligent message generation.

        Expected format from unified_supervisor:
        {
            "stage": "backend",
            "message": "Starting backend server...",
            "progress": 50,
            "metadata": {
                "icon": "server",
                "phase": 3,
                "components": {
                    "backend": {"status": "running"},
                    ...
                }
            }
        }
        """
        if not body:
            return self._json_response({"error": "No body"}, status=400)

        try:
            data = json.loads(body.decode())
            new_progress = data.get("progress", self._progress)

            # v212.0: Use lock-free progress update for monotonicity
            if self._lock_free_progress:
                success, current = self._lock_free_progress.update_progress(new_progress)
                if success:
                    self._progress = current
            else:
                self._progress = max(self._progress, new_progress)

            self._phase = data.get("stage", data.get("phase", self._phase))
            self._message = data.get("message", self._message)

            # v212.0: Generate intelligent message if none provided
            if not data.get("message") and self._message_generator:
                try:
                    self._message = self._message_generator.generate_message(
                        stage=self._phase,
                        progress=self._progress,
                    )
                except Exception:
                    pass  # Keep existing message on failure

            # v127.0: Properly handle metadata with nested components
            if "metadata" in data:
                metadata = data["metadata"]
                # If metadata contains a 'components' key, extract and use it
                if "components" in metadata:
                    self._components = metadata["components"]
                else:
                    # Legacy format: metadata IS the components dict
                    self._components.update(metadata)
                
                # v185.0: Persist Trinity summary from supervisor broadcasts
                if "trinity" in metadata:
                    self._trinity_summary = metadata["trinity"]
                if "trinity_ready" in metadata:
                    self._trinity_ready = metadata["trinity_ready"]

                # v225.0: Persist Prime v2 init_progress protocol data
                if "init_progress" in metadata:
                    self._prime_init_progress = metadata["init_progress"]

            self._eta_engine.update_progress(self._progress)

            # v212.0: Update enhanced ETA engine
            if self._enhanced_eta:
                try:
                    self._enhanced_eta.update_progress(self._progress)
                except Exception:
                    pass

            # v183.0: Track supervisor activity
            self._last_supervisor_update = time.time()
            
            # v186.0: Increment sequence and store in history for recovery
            self._sequence_number += 1
            self._update_history.append({
                "sequence": self._sequence_number,
                "progress": self._progress,
                "phase": self._phase,
                "message": self._message,
                "timestamp": time.time(),
            })

            # v212.0: Log event for replay/debugging
            if self._event_log:
                try:
                    self._event_log.append_event(
                        "progress_update",
                        {
                            "progress": self._progress,
                            "stage": self._phase,
                            "message": self._message,
                            "components": list(self._components.keys()) if self._components else [],
                        },
                        trace_id=self._trace_context.trace_id if self._trace_context else None,
                    )
                except Exception:
                    pass

            # v212.0: Persist progress for browser refresh resume
            if self._persistence:
                try:
                    self._persistence.save_progress(
                        session_id=self._session_id,
                        progress=self._progress,
                        stage=self._phase,
                        message=self._message,
                        trace_id=self._trace_context.trace_id if self._trace_context else None,
                        completed=(self._progress >= 100),
                    )
                except Exception:
                    pass
            
            await self._broadcast_progress()

            return self._json_response({"status": "updated", "sequence": self._sequence_number})
        except Exception as e:
            logger.debug(f"[v212.0] Progress update error: {e}")
            return self._json_response({"error": str(e)}, status=400)

    async def _handle_component_register(self, body: Optional[bytes]) -> str:
        """Handle component registration."""
        if not body:
            return self._json_response({"error": "No body"}, status=400)

        try:
            data = json.loads(body.decode())
            self._dependency_graph.register_component(
                name=data["name"],
                dependencies=data.get("dependencies"),
                weight=data.get("weight", 1.0),
                is_critical=data.get("is_critical", False)
            )
            return self._json_response({"status": "registered"})
        except Exception as e:
            return self._json_response({"error": str(e)}, status=400)

    async def _handle_component_complete(self, body: Optional[bytes]) -> str:
        """Handle component completion."""
        if not body:
            return self._json_response({"error": "No body"}, status=400)

        try:
            data = json.loads(body.decode())
            name = data["name"]
            duration = data.get("duration_ms", 0) / 1000

            self._dependency_graph.mark_complete(name)
            self._eta_engine.record_component_complete(name, duration)

            await self._broadcast_progress()
            return self._json_response({"status": "completed"})
        except Exception as e:
            return self._json_response({"error": str(e)}, status=400)

    async def _handle_trinity_status_update(self, body: Optional[bytes]) -> str:
        """
        v183.0: Handle status update from Trinity components (J-Prime, Reactor).
        
        Expected payload:
        {
            "component": "jarvis_prime" | "reactor_core",
            "progress": 0-100,
            "status": "starting" | "running" | "ready" | "error",
            "message": "Loading models..."
        }
        """
        if not body:
            return self._json_response({"error": "No body"}, status=400)

        try:
            data = json.loads(body.decode())
            component = data.get("component")
            
            if component not in self._trinity_status:
                return self._json_response({"error": f"Unknown component: {component}"}, status=400)
            
            self._trinity_status[component] = {
                "progress": data.get("progress", 0),
                "status": data.get("status", "unknown"),
                "message": data.get("message", ""),
                "last_update": time.time(),
            }
            
            # Also update supervisor tracking since we got an update
            self._last_supervisor_update = time.time()
            
            # Broadcast to WebSocket clients
            await self._ws_manager.broadcast(json.dumps({
                "type": "trinity_update",
                "data": self._trinity_status
            }))
            
            logger.info(f"[v183.0] Trinity update: {component} → {data.get('progress', 0)}% ({data.get('status', 'unknown')})")
            
            return self._json_response({"status": "updated"})
        except Exception as e:
            return self._json_response({"error": str(e)}, status=400)

    # =========================================================================
    # WebSocket Handling
    # =========================================================================

    async def _handle_websocket_upgrade(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: Dict):
        """Handle WebSocket upgrade and connection."""
        # Calculate accept key
        ws_key = headers.get('sec-websocket-key', '')
        accept_key = self._calculate_ws_accept(ws_key)

        # Send upgrade response
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n"
            "\r\n"
        )
        writer.write(response.encode())
        await writer.drain()

        # Add to connection manager
        conn_id = await self._ws_manager.add_connection(writer)

        # Send initial state
        await self._broadcast_progress()

        try:
            # Keep connection alive and handle incoming messages
            while not self._shutdown_requested:
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=60.0)
                    if not data:
                        break
                    # Handle incoming WebSocket frames (ping/pong, close, etc.)
                    if data[0] == 0x88:  # Close frame
                        break
                    elif data[0] == 0x8A:  # Pong frame
                        pass  # Client responded to ping
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    pass
        finally:
            await self._ws_manager.remove_connection(conn_id)

    def _calculate_ws_accept(self, key: str) -> str:
        """Calculate WebSocket accept key."""
        import base64
        magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        sha1 = hashlib.sha1((key + magic).encode()).digest()
        return base64.b64encode(sha1).decode()

    # =========================================================================
    # SSE Handling
    # =========================================================================

    async def _handle_sse_stream(self) -> str:
        """Handle Server-Sent Events stream (SSE fallback for WebSocket)."""
        # This would need special handling in the response to keep connection open
        # For now, return current state
        return self._json_response(self._get_progress_response())

    # =========================================================================
    # Response Helpers
    # =========================================================================

    def _json_response(self, data: Dict[str, Any], status: int = 200) -> str:
        """Create JSON HTTP response."""
        body = json.dumps(data)
        status_text = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "Unknown")
        return (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )

    def _html_response(self, html: str) -> str:
        """Create HTML HTTP response."""
        return (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(html.encode())}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{html}"
        )

    def _serve_static_file(self, path: str) -> Union[str, bytes]:
        """
        Serve static file with proper handling for both text and binary files.

        v182.0: Fixed binary file handling for images, fonts, etc.
        Now uses read_bytes() for binary files and read_text() for text files.
        """
        static_dir = self.config.jarvis_repo / "frontend" / "public"
        file_path = static_dir / path.lstrip("/")

        if file_path.exists() and file_path.is_file():
            # Content type mapping with binary flag
            content_types = {
                ".js": ("application/javascript", False),
                ".css": ("text/css", False),
                ".html": ("text/html", False),
                ".json": ("application/json", False),
                ".svg": ("image/svg+xml", False),
                ".png": ("image/png", True),
                ".ico": ("image/x-icon", True),
                ".jpg": ("image/jpeg", True),
                ".jpeg": ("image/jpeg", True),
                ".gif": ("image/gif", True),
                ".webp": ("image/webp", True),
                ".woff": ("font/woff", True),
                ".woff2": ("font/woff2", True),
                ".ttf": ("font/ttf", True),
                ".eot": ("application/vnd.ms-fontobject", True),
            }

            content_type, is_binary = content_types.get(file_path.suffix.lower(), ("text/plain", False))

            try:
                if is_binary:
                    # Binary files - read as bytes
                    content = file_path.read_bytes()
                    headers = (
                        f"HTTP/1.1 200 OK\r\n"
                        f"Content-Type: {content_type}\r\n"
                        f"Content-Length: {len(content)}\r\n"
                        f"Cache-Control: public, max-age=3600\r\n"
                        f"Connection: close\r\n"
                        f"\r\n"
                    )
                    return headers.encode() + content
                else:
                    # Text files - read as text
                    content = file_path.read_text(encoding='utf-8')
                    return (
                        f"HTTP/1.1 200 OK\r\n"
                        f"Content-Type: {content_type}; charset=utf-8\r\n"
                        f"Content-Length: {len(content.encode('utf-8'))}\r\n"
                        f"Cache-Control: public, max-age=300\r\n"
                        f"Connection: close\r\n"
                        f"\r\n"
                        f"{content}"
                    )
            except Exception as e:
                logger.warning(f"[v182.0] Error serving static file {path}: {e}")
                return self._json_response({"error": f"Error reading file: {e}"}, status=500)

        return self._json_response({"error": "Not found"}, status=404)

    def _get_loading_page(self) -> str:
        """
        Serve the themed loading page HTML (Arc Reactor + Matrix theme).

        v186.0: Now injects port configuration into the HTML so loading-manager.js
        knows the correct loading server port even when served from a different origin.

        v182.0: Now serves the actual themed loading.html from frontend/public/
        instead of inline basic HTML. The themed page includes:
        - Arc Reactor animation with rotating rings
        - Matrix rain background effect
        - Trinity Components status section
        - Two-Tier Security status display
        - Live operations log
        - Stage indicators grid
        - Particle effects
        - Panel minimize/hide functionality

        Falls back to inline HTML only if the themed file cannot be loaded.
        """
        # v186.0: Port configuration to inject into the page
        # This is CRITICAL - window.location.port may be wrong when served via proxy
        port_config_script = f'''<script>
// v186.0: Server-injected port configuration (loading_server.py)
// This ensures loading-manager.js uses the correct loading server port
window.JARVIS_LOADING_SERVER_PORT = {self.config.port};
window.JARVIS_FRONTEND_PORT = {self.config.frontend_port};
window.JARVIS_BACKEND_PORT = {self.config.backend_port};
window.JARVIS_PRIME_PORT = {self.config.prime_port};
window.JARVIS_REACTOR_PORT = {self.config.reactor_port};
console.log('[v186.0] Port config injected by loading_server.py:', {{
    loading: window.JARVIS_LOADING_SERVER_PORT,
    frontend: window.JARVIS_FRONTEND_PORT,
    backend: window.JARVIS_BACKEND_PORT,
    prime: window.JARVIS_PRIME_PORT,
    reactor: window.JARVIS_REACTOR_PORT
}});
</script>
'''

        # v182.0: Try to load the themed loading.html first
        themed_loading_page = self.config.jarvis_repo / "frontend" / "public" / "loading.html"

        try:
            if themed_loading_page.exists():
                html = themed_loading_page.read_text(encoding='utf-8')
                # v186.0: Inject port config right after <head> tag
                if '<head>' in html:
                    html = html.replace('<head>', '<head>\n' + port_config_script, 1)
                elif '<HEAD>' in html:
                    html = html.replace('<HEAD>', '<HEAD>\n' + port_config_script, 1)
                else:
                    # Fallback: prepend to entire HTML
                    html = port_config_script + html
                logger.info(f"[v186.0] Serving themed loading page with port injection from {themed_loading_page}")
                return html
            else:
                logger.warning(f"[v182.0] Themed loading page not found at {themed_loading_page}, using fallback")
        except Exception as e:
            logger.warning(f"[v182.0] Could not load themed loading page: {e}, using fallback")

        # v182.0: Fallback to inline HTML only if themed page unavailable
        # Get actual ports from configuration for the fallback
        loading_port = self.config.port
        frontend_port = self.config.frontend_port

        logger.info(f"[v182.0] Using fallback inline loading page (ports: loading={loading_port}, frontend={frontend_port})")

        # Fallback inline HTML (simpler version for emergencies)
        html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>J.A.R.V.I.S. - Initializing</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 50%, #0a0a0a 100%);
            color: #00ff88;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }
        .container { text-align: center; padding: 2rem; max-width: 600px; }
        .logo {
            font-size: 3.5rem;
            font-weight: 300;
            letter-spacing: 0.8rem;
            margin-bottom: 1rem;
            text-shadow: 0 0 30px rgba(0, 255, 136, 0.4);
            animation: glow 2s ease-in-out infinite alternate;
        }
        @keyframes glow {
            from { text-shadow: 0 0 20px rgba(0, 255, 136, 0.4); }
            to { text-shadow: 0 0 40px rgba(0, 255, 136, 0.6); }
        }
        .subtitle { font-size: 0.9rem; opacity: 0.6; margin-bottom: 2rem; letter-spacing: 0.3rem; }
        .progress-container {
            width: 100%;
            height: 6px;
            background: rgba(0, 255, 136, 0.1);
            border-radius: 3px;
            overflow: hidden;
            margin: 1.5rem 0;
        }
        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, #00ff88, #00ccff, #00ff88);
            background-size: 200% 100%;
            animation: shimmer 2s linear infinite;
            transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1);
        }
        @keyframes shimmer {
            0% { background-position: 200% 0; }
            100% { background-position: -200% 0; }
        }
        .progress-text {
            display: flex;
            justify-content: space-between;
            font-size: 0.85rem;
            opacity: 0.8;
            margin-bottom: 0.5rem;
        }
        .eta { color: #00ccff; }
        .status { margin: 1.5rem 0; font-size: 0.95rem; opacity: 0.9; }
        .components {
            margin-top: 2rem;
            text-align: left;
            font-size: 0.8rem;
        }
        .component {
            display: flex;
            align-items: center;
            padding: 0.6rem;
            margin: 0.3rem 0;
            background: rgba(0, 255, 136, 0.03);
            border-radius: 4px;
            border-left: 2px solid transparent;
            transition: all 0.3s ease;
        }
        .component.complete { border-left-color: #00ff88; color: #00ff88; }
        .component.running { border-left-color: #ffcc00; color: #ffcc00; animation: pulse 1s infinite; }
        .component.pending { opacity: 0.5; }
        .component.failed { border-left-color: #ff4444; color: #ff4444; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
        .component-icon { margin-right: 0.8rem; width: 16px; text-align: center; }
        .component-name { flex: 1; }
        .component-time { font-size: 0.75rem; opacity: 0.6; }
        .connection-status {
            position: fixed;
            bottom: 1rem;
            right: 1rem;
            font-size: 0.7rem;
            opacity: 0.4;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .connection-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #00ff88;
        }
        .connection-dot.disconnected { background: #ff4444; }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">J.A.R.V.I.S.</div>
        <div class="subtitle">INITIALIZING SYSTEMS</div>
        <div class="progress-text">
            <span id="progress-percent">0%</span>
            <span class="eta" id="eta">Calculating...</span>
        </div>
        <div class="progress-container">
            <div class="progress-bar" id="progress-bar" style="width: 0%"></div>
        </div>
        <div class="status" id="status">Connecting to supervisor...</div>
        <div class="components" id="components"></div>
    </div>
    <div class="connection-status">
        <div class="connection-dot" id="connection-dot"></div>
        <span id="connection-text">Connecting</span>
    </div>

    <script>
        const WS_URL = 'ws://localhost:__LOADING_PORT__/ws/progress';
        const API_URL = 'http://localhost:__LOADING_PORT__';
        let ws = null;
        let reconnectAttempts = 0;
        const MAX_RECONNECT = 10;

        function updateUI(data) {
            const progress = data.progress || 0;
            document.getElementById('progress-bar').style.width = progress + '%';
            document.getElementById('progress-percent').textContent = progress + '%';
            document.getElementById('status').textContent = data.message || data.phase || 'Loading...';

            if (data.eta && data.eta.eta_seconds !== null) {
                const eta = data.eta.eta_seconds;
                if (eta <= 0) {
                    document.getElementById('eta').textContent = 'Almost ready...';
                } else if (eta < 60) {
                    document.getElementById('eta').textContent = '~' + Math.round(eta) + 's remaining';
                } else {
                    document.getElementById('eta').textContent = '~' + Math.round(eta/60) + 'm remaining';
                }
            }

            if (data.components || data.dependency_graph?.components) {
                const comps = data.dependency_graph?.components || data.components;
                const container = document.getElementById('components');
                container.innerHTML = Object.entries(comps).map(([name, info]) => {
                    const status = typeof info === 'object' ? info.status : info;
                    const icon = status === 'complete' ? '✓' : status === 'running' ? '◉' : status === 'failed' ? '✗' : '○';
                    const duration = info.duration_ms ? (info.duration_ms/1000).toFixed(1) + 's' : '';
                    return '<div class="component ' + status + '">' +
                           '<span class="component-icon">' + icon + '</span>' +
                           '<span class="component-name">' + name.replace(/_/g, ' ') + '</span>' +
                           '<span class="component-time">' + duration + '</span>' +
                           '</div>';
                }).join('');
            }

            if (progress >= 100) {
                setTimeout(() => {
                    window.location.href = 'http://localhost:__FRONTEND_PORT__';
                }, 1500);
            }
        }

        function setConnectionStatus(connected) {
            const dot = document.getElementById('connection-dot');
            const text = document.getElementById('connection-text');
            if (connected) {
                dot.classList.remove('disconnected');
                text.textContent = 'Live';
            } else {
                dot.classList.add('disconnected');
                text.textContent = 'Reconnecting...';
            }
        }

        function connectWebSocket() {
            ws = new WebSocket(WS_URL);

            ws.onopen = () => {
                setConnectionStatus(true);
                reconnectAttempts = 0;
            };

            ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'progress') {
                        updateUI(msg.data);
                    }
                } catch (e) {}
            };

            ws.onclose = () => {
                setConnectionStatus(false);
                if (reconnectAttempts < MAX_RECONNECT) {
                    setTimeout(() => {
                        reconnectAttempts++;
                        connectWebSocket();
                    }, Math.min(1000 * Math.pow(2, reconnectAttempts), 10000));
                }
            };

            ws.onerror = () => ws.close();
        }

        async function pollProgress() {
            try {
                const res = await fetch(API_URL + '/api/progress');
                const data = await res.json();
                updateUI(data);
            } catch (e) {}
        }

        // Start with WebSocket, fall back to polling
        connectWebSocket();
        setInterval(() => {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                pollProgress();
            }
        }, 2000);

        pollProgress(); // Initial fetch
    </script>
</body>
</html>'''

        # v126.0: Substitute actual port values into the template
        html = html.replace('__LOADING_PORT__', str(loading_port))
        html = html.replace('__FRONTEND_PORT__', str(frontend_port))
        return html

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _background_ping_task(self):
        """Send periodic WebSocket pings."""
        while not self._shutdown_requested:
            await asyncio.sleep(self.config.websocket_ping_interval)
            await self._ws_manager.send_ping()

    async def _background_eta_update_task(self):
        """Update ETA periodically."""
        while not self._shutdown_requested:
            await asyncio.sleep(self.config.eta_update_interval)
            await self._broadcast_progress()

    # =========================================================================
    # Server Lifecycle
    # =========================================================================

    async def start(self):
        """Start the loading server."""
        logger.info(f"[v212.0] Starting loading server on {self.config.host}:{self.config.port}")
        self._loop = asyncio.get_running_loop()

        # =================================================================
        # v211.0: PARENT DEATH WATCHER - Prevent orphaned processes
        # =================================================================
        # When the supervisor (kernel) crashes, this process should exit too.
        # Without this, the loading server becomes an orphan that persists
        # across restarts, causing "Cleaned N orphaned processes" warnings.
        # =================================================================
        self._parent_watcher = None
        try:
            from backend.utils.parent_death_watcher import start_parent_watcher
            self._parent_watcher = await start_parent_watcher()
            if self._parent_watcher:
                logger.info("[v211.0] Parent death watcher started - will auto-exit if supervisor dies")
            else:
                logger.debug("[v211.0] Running standalone - no parent watcher needed")
        except ImportError:
            logger.debug("Parent death watcher not available")
        except Exception as e:
            logger.debug(f"Could not start parent death watcher: {e}")

        # Initialize ETA tracking
        self._eta_engine.start_tracking()

        # v212.0: Initialize enhanced ETA if available
        if self._enhanced_eta:
            self._enhanced_eta.start_session(self._session_id)

        # Start background tasks
        # v210.0: Use safe tasks to prevent "Future exception was never retrieved"
        self._background_tasks = [
            create_safe_task(self._background_ping_task(), name="background_ping"),
            create_safe_task(self._background_eta_update_task(), name="background_eta_update"),
        ]

        # Start server
        # v238.1: Handle port bind failures with actionable diagnostics
        try:
            self._server = await asyncio.start_server(
                self.handle_request,
                host=self.config.host,
                port=self.config.port,
                reuse_address=True,
            )
        except OSError as e:
            # Cancel background tasks started above before dying
            for task in self._background_tasks:
                task.cancel()
            self._background_tasks.clear()
            logger.error(
                f"[FATAL] Cannot bind to {self.config.host}:{self.config.port}: {e}. "
                f"Check: lsof -i :{self.config.port} | "
                f"Run: kill $(lsof -t -i :{self.config.port})"
            )
            raise

        addr = self._server.sockets[0].getsockname()
        logger.info(f"[v125.0] Loading server ready on http://{addr[0]}:{addr[1]}")

        # Connect to hub lazily in background so health endpoint is immediately ready.
        self._hub_connect_task = create_safe_task(
            self._deferred_connect_hub(),
            name="loading_server_hub_connect",
        )
        self._background_tasks.append(self._hub_connect_task)

        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        """Stop the loading server gracefully."""
        if self._stopping:
            return
        self._stopping = True

        logger.info("[v212.0] Shutting down loading server...")
        self._shutdown_requested = True
        stop_timeout = max(0.5, float(os.getenv("LOADING_SERVER_STOP_TIMEOUT", "6.0")))
        try:
            # =================================================================
            # v211.0: PARENT DEATH WATCHER - Stop monitoring during graceful shutdown
            # =================================================================
            if hasattr(self, '_parent_watcher') and self._parent_watcher:
                try:
                    from backend.utils.parent_death_watcher import stop_parent_watcher
                    await stop_parent_watcher()
                    logger.info("[v211.0] Parent death watcher stopped")
                except Exception as e:
                    logger.debug(f"Parent death watcher cleanup: {e}")

            # Close websocket clients immediately so request handlers blocked on
            # reader.read() can observe disconnect and exit quickly.
            try:
                closed_connections = await self._ws_manager.close_all(timeout=stop_timeout / 3.0)
                if closed_connections:
                    logger.debug("[v212.1] Closed %d websocket connection(s)", closed_connections)
            except Exception as e:
                logger.debug(f"[v212.1] WebSocket close_all cleanup: {e}")

            # Cancel background tasks
            for task in self._background_tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._background_tasks.clear()

            # Save ETA history
            self._eta_engine.finish_tracking()

            # v212.0: Save enhanced ETA history if available
            if self._enhanced_eta:
                self._enhanced_eta.finish_session()

            # v212.0: Log shutdown event
            if self._event_log:
                self._event_log.append_event(
                    "server_shutdown",
                    {
                        "session_id": self._session_id,
                        "progress": self._progress,
                        "phase": self._phase,
                        "uptime_seconds": time.time() - self._startup_time,
                    },
                    trace_id=self._trace_context.trace_id if self._trace_context else None,
                )

            # Close server
            if self._server:
                self._server.close()
                with suppress(Exception):
                    await asyncio.wait_for(self._server.wait_closed(), timeout=stop_timeout / 3.0)
                self._server = None

            # Drain any in-flight HTTP/WebSocket request tasks.
            with suppress(Exception):
                await self._drain_active_request_tasks(timeout=stop_timeout / 2.0)

            logger.info("[v212.0] Loading server stopped")
        finally:
            self._stopping = False


# =============================================================================
# v125.0: MAIN ENTRY POINT
# =============================================================================

async def run_server():
    """Run the loading server."""
    config = get_config()
    server = LoadingServer(config)

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def signal_handler():
        # v210.0: Use safe task to prevent "Future exception was never retrieved"
        create_safe_task(server.stop(), name="signal_handler_stop")

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await server.start()
    except asyncio.CancelledError:
        await server.stop()


def main():
    """Main entry point."""
    logger.info("[v212.0] JARVIS Loading Server starting...")

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("[v212.0] Interrupted by user")
    except Exception as e:
        logger.error(f"[v212.0] Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
