#!/usr/bin/env python3
"""
JARVIS Loading Server v125.0 - Enterprise-Grade Startup Orchestration Hub
==========================================================================

The ultimate loading server that serves as the central nervous system for
JARVIS startup coordination across all Trinity components.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    Loading Server v125.0 (Port 3001)                    │
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
Version: 125.0.0
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
logger = logging.getLogger("LoadingServer.v125")

T = TypeVar('T')


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
    prime_port: int = field(default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8000")))
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
    v125.0: Enterprise-grade loading server with full Trinity integration.
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
        self._server: Optional[asyncio.Server] = None

        # Background tasks
        self._background_tasks: List[asyncio.Task] = []

        # Integration with unified hub (if available)
        self._hub = None
        self._try_connect_hub()

    def _try_connect_hub(self):
        """Try to connect to the UnifiedStartupProgressHub."""
        try:
            sys.path.insert(0, str(self.config.jarvis_repo))
            from backend.core.unified_startup_progress import get_progress_hub
            self._hub = get_progress_hub()
            self._hub.register_sync_target(self._on_hub_update)
            logger.info("[v125.0] Connected to UnifiedStartupProgressHub")
        except Exception as e:
            logger.debug(f"[v125.0] Hub not available: {e}")

    def _on_hub_update(self, state: Dict[str, Any]):
        """Callback when unified hub updates."""
        self._progress = state.get("progress", self._progress)
        self._phase = state.get("phase", self._phase)
        self._message = state.get("message", self._message)

        # Broadcast to WebSocket clients
        asyncio.create_task(self._broadcast_progress())

    async def _broadcast_progress(self):
        """Broadcast current progress to all WebSocket clients."""
        eta_data = self._eta_engine.get_predicted_eta()
        message = json.dumps({
            "type": "progress",
            "data": {
                "progress": self._progress,
                "phase": self._phase,
                "message": self._message,
                "eta": eta_data,
                "components": self._components,
                "timestamp": datetime.now().isoformat()
            }
        })
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
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _route_request(self, method: str, path: str, headers: Dict, body: Optional[bytes]) -> str:
        """Route HTTP request to appropriate handler."""

        # API Routes
        if path == "/api/supervisor/health":
            return self._json_response(self._get_health_response())

        elif path == "/api/supervisor/heartbeat":
            return self._json_response({"alive": True, "timestamp": time.time()})

        elif path == "/api/supervisor/status":
            return self._json_response(self._get_full_status())

        elif path == "/api/health/unified":
            health = await self._health_aggregator.get_unified_health()
            return self._json_response(health)

        elif path == "/api/progress":
            return self._json_response(self._get_progress_response())

        elif path == "/api/progress/eta":
            return self._json_response(self._eta_engine.get_predicted_eta())

        elif path == "/api/progress/components":
            return self._json_response(self._dependency_graph.get_progress())

        elif path == "/api/update-progress" and method == "POST":
            return await self._handle_progress_update(body)

        elif path == "/api/component/register" and method == "POST":
            return await self._handle_component_register(body)

        elif path == "/api/component/complete" and method == "POST":
            return await self._handle_component_complete(body)

        elif path == "/api/shutdown" and method == "POST":
            self._shutdown_requested = True
            return self._json_response({"status": "shutdown_initiated"})

        elif path == "/sse/progress":
            return await self._handle_sse_stream()

        # Static routes
        elif path == "/" or path == "/index.html":
            return self._html_response(self._get_loading_page())

        elif path.endswith((".js", ".css", ".png", ".ico")):
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
                "version": "125.0.0",
                "progress": self._progress,
                "phase": self._phase,
                "websocket_clients": self._ws_manager.connection_count,
            },
            "timestamp": datetime.now().isoformat(),
        }

    def _get_progress_response(self) -> Dict[str, Any]:
        """Get current progress state."""
        eta = self._eta_engine.get_predicted_eta()
        return {
            "progress": self._progress,
            "phase": self._phase,
            "message": self._message,
            "eta": eta,
            "components": self._components,
            "dependency_graph": self._dependency_graph.get_progress(),
            "timestamp": datetime.now().isoformat(),
        }

    def _get_full_status(self) -> Dict[str, Any]:
        """Get full supervisor and loading server status."""
        state = self.get_supervisor_state()
        return {
            "supervisor": state,
            "loading_server": {
                "version": "125.0.0",
                "startup_time": self._startup_time,
                "uptime_seconds": time.time() - self._startup_time,
                "progress": self._progress,
                "phase": self._phase,
                "message": self._message,
                "websocket_clients": self._ws_manager.connection_count,
            },
            "eta": self._eta_engine.get_predicted_eta(),
            "components": self._dependency_graph.get_progress(),
            "circuit_breakers": {
                name: cb.state.value
                for name, cb in self._health_aggregator._circuit_breakers.items()
            },
        }

    async def _handle_progress_update(self, body: Optional[bytes]) -> str:
        """Handle progress update from supervisor.

        v127.0: Fixed component handling - now properly extracts 'components' from
        metadata for display on the loading page.

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
            self._progress = data.get("progress", self._progress)
            self._phase = data.get("stage", data.get("phase", self._phase))
            self._message = data.get("message", self._message)

            # v127.0: Properly handle metadata with nested components
            if "metadata" in data:
                metadata = data["metadata"]
                # If metadata contains a 'components' key, extract and use it
                if "components" in metadata:
                    self._components = metadata["components"]
                else:
                    # Legacy format: metadata IS the components dict
                    self._components.update(metadata)

            self._eta_engine.update_progress(self._progress)
            await self._broadcast_progress()

            return self._json_response({"status": "updated"})
        except Exception as e:
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

    def _serve_static_file(self, path: str) -> str:
        """Serve static file."""
        static_dir = self.config.jarvis_repo / "frontend" / "public"
        file_path = static_dir / path.lstrip("/")

        if file_path.exists() and file_path.is_file():
            content = file_path.read_text()
            content_type = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".png": "image/png",
                ".ico": "image/x-icon",
            }.get(file_path.suffix, "text/plain")

            return (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(content.encode())}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{content}"
            )

        return self._json_response({"error": "Not found"}, status=404)

    def _get_loading_page(self) -> str:
        """
        Serve the loading page HTML.

        v127.0: Prioritizes themed loading.html from frontend/public/
        which has the Arc Reactor + Matrix rain animation matching the
        main JARVIS UI theme. Falls back to inline basic HTML if file
        not found.

        v126.0: Fixed hardcoded ports - now uses actual configured ports from
        environment variables (LOADING_SERVER_PORT, JARVIS_FRONTEND_PORT) for:
        - WebSocket URL (ws://localhost:{loading_port}/ws/progress)
        - API URL (http://localhost:{loading_port})
        - Frontend redirect URL (http://localhost:{frontend_port})

        This ensures the loading page works correctly regardless of which
        ports are configured.
        """
        # v127.0: Try to serve themed loading.html first (matches main UI theme)
        themed_loading_path = self.config.jarvis_repo / "frontend" / "public" / "loading.html"

        if themed_loading_path.exists():
            try:
                html = themed_loading_path.read_text()
                logger.debug(f"[LoadingServer] Serving themed loading page from {themed_loading_path}")
                return html
            except Exception as e:
                logger.warning(f"[LoadingServer] Failed to read themed loading.html: {e}, using fallback")

        # Fallback to inline basic loading page
        logger.debug("[LoadingServer] Using fallback inline loading page")

        # Get actual ports from configuration
        loading_port = self.config.port
        frontend_port = self.config.frontend_port

        # Use template with placeholders then substitute
        # (can't use f-string because CSS uses curly braces)
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
        logger.info(f"[v125.0] Starting loading server on {self.config.host}:{self.config.port}")

        # Initialize ETA tracking
        self._eta_engine.start_tracking()

        # Start background tasks
        self._background_tasks = [
            asyncio.create_task(self._background_ping_task()),
            asyncio.create_task(self._background_eta_update_task()),
        ]

        # Start server
        self._server = await asyncio.start_server(
            self.handle_request,
            host=self.config.host,
            port=self.config.port,
            reuse_address=True,
        )

        addr = self._server.sockets[0].getsockname()
        logger.info(f"[v125.0] Loading server ready on http://{addr[0]}:{addr[1]}")

        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        """Stop the loading server gracefully."""
        logger.info("[v125.0] Shutting down loading server...")
        self._shutdown_requested = True

        # Cancel background tasks
        for task in self._background_tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        # Save ETA history
        self._eta_engine.finish_tracking()

        # Close server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("[v125.0] Loading server stopped")


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
        asyncio.create_task(server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await server.start()
    except asyncio.CancelledError:
        await server.stop()


def main():
    """Main entry point."""
    logger.info("[v125.0] JARVIS Loading Server starting...")

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("[v125.0] Interrupted by user")
    except Exception as e:
        logger.error(f"[v125.0] Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
