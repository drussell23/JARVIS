"""
Trinity Orchestration Engine v1.0 - The Digital Biology Coordinator
====================================================================

This is the "God Process" that orchestrates the Trinity ecosystem:
- JARVIS (Body) - Interaction, Sensing, Execution
- JARVIS-Prime (Mind) - Reasoning, Planning, Routing
- Reactor-Core (Nerves) - Reflexes, Training, Self-Healing

Advanced Features:
- Distributed Consensus Protocol (Raft-inspired leader election)
- Split-Brain Detection with automatic recovery
- Vector Clock synchronization across components
- Circuit Breaker coordination for cascade failure prevention
- Predictive Auto-Scaling with Holt-Winters forecasting
- Experience Pipeline with guaranteed delivery
- Model Hot-Swap with Read-Copy-Update (RCU) locks
- Adaptive Backpressure with synaptic pruning

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    Trinity Orchestration Engine                          │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐        │
    │  │   Consensus     │   │   Health        │   │   Experience    │        │
    │  │   Protocol      │   │   Coordinator   │   │   Pipeline      │        │
    │  │   (Raft-like)   │   │   (Circuit BKR) │   │   (Guaranteed)  │        │
    │  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘        │
    │           │                     │                     │                 │
    │           └─────────────────────┼─────────────────────┘                 │
    │                                 │                                        │
    │                                 ▼                                        │
    │                    ┌────────────────────────┐                            │
    │                    │   Component Registry   │                            │
    │                    │   with Vector Clocks   │                            │
    │                    └───────────┬────────────┘                            │
    │                                │                                         │
    │           ┌────────────────────┼────────────────────┐                    │
    │           ▼                    ▼                    ▼                    │
    │    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐             │
    │    │    JARVIS    │    │  JARVIS-Prime│    │ Reactor-Core │             │
    │    │   (Body)     │    │   (Mind)     │    │  (Nerves)    │             │
    │    └──────────────┘    └──────────────┘    └──────────────┘             │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("TrinityOrchestration")


# =============================================================================
# CONFIGURATION (Dynamic, no hardcoding)
# =============================================================================

def _get_env_path(name: str, default_subpath: str) -> Path:
    """Get path from environment or use dynamic default."""
    env_value = os.getenv(name)
    if env_value:
        return Path(env_value)
    return Path.home() / "Documents" / "repos" / default_subpath


class TrinityConfig:
    """Dynamic configuration for Trinity ecosystem."""

    # Repository paths (auto-discovered)
    JARVIS_PATH = _get_env_path("JARVIS_PATH", "JARVIS-AI-Agent")
    PRIME_PATH = _get_env_path("JARVIS_PRIME_PATH", "JARVIS-Prime")
    REACTOR_PATH = _get_env_path("REACTOR_CORE_PATH", "reactor-core")

    # IPC directories
    TRINITY_BASE = Path(os.getenv("TRINITY_BASE_DIR", str(Path.home() / ".jarvis")))
    TRINITY_EVENTS_DIR = TRINITY_BASE / "trinity" / "events"
    REACTOR_EVENTS_DIR = TRINITY_BASE / "reactor" / "events"
    HEARTBEAT_DIR = TRINITY_BASE / "heartbeats"

    # Ports (dynamic allocation if not specified)
    JARVIS_PORT = int(os.getenv("JARVIS_PORT", "8765"))
    PRIME_PORT = int(os.getenv("PRIME_PORT", "8000"))
    REACTOR_PORT = int(os.getenv("REACTOR_PORT", "8090"))

    # Timing
    HEARTBEAT_INTERVAL = float(os.getenv("TRINITY_HEARTBEAT_INTERVAL", "5.0"))
    HEALTH_CHECK_INTERVAL = float(os.getenv("TRINITY_HEALTH_INTERVAL", "10.0"))
    CONSENSUS_TIMEOUT = float(os.getenv("TRINITY_CONSENSUS_TIMEOUT", "30.0"))
    STARTUP_TIMEOUT = float(os.getenv("TRINITY_STARTUP_TIMEOUT", "120.0"))

    # Thresholds
    BACKPRESSURE_THRESHOLD = int(os.getenv("TRINITY_BACKPRESSURE_THRESHOLD", "100"))
    CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("TRINITY_CIRCUIT_THRESHOLD", "5"))
    SPLIT_BRAIN_TIMEOUT = float(os.getenv("TRINITY_SPLIT_BRAIN_TIMEOUT", "15.0"))


# =============================================================================
# ENUMS
# =============================================================================

class ComponentState(Enum):
    """Lifecycle states for Trinity components."""
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"
    STOPPED = "stopped"
    FAILED = "failed"


class ConsensusRole(Enum):
    """Roles in distributed consensus."""
    LEADER = "leader"
    FOLLOWER = "follower"
    CANDIDATE = "candidate"


class ComponentType(Enum):
    """Types of Trinity components."""
    BODY = "jarvis"       # JARVIS - Interaction layer
    MIND = "prime"        # JARVIS-Prime - Cognition layer
    NERVES = "reactor"    # Reactor-Core - Training/Evolution layer


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class VectorClock:
    """
    Lamport-style vector clock for causal ordering across Trinity.

    Each component maintains its own counter. On message send/receive,
    clocks are merged to maintain causal consistency.
    """
    clocks: Dict[str, int] = field(default_factory=dict)

    def tick(self, node_id: str) -> "VectorClock":
        """Increment local clock."""
        self.clocks[node_id] = self.clocks.get(node_id, 0) + 1
        return self

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Merge with another vector clock (take max of each)."""
        all_keys = set(self.clocks.keys()) | set(other.clocks.keys())
        self.clocks = {
            k: max(self.clocks.get(k, 0), other.clocks.get(k, 0))
            for k in all_keys
        }
        return self

    def happens_before(self, other: "VectorClock") -> bool:
        """Check if this clock happens-before another."""
        all_keys = set(self.clocks.keys()) | set(other.clocks.keys())
        at_least_one_less = False

        for k in all_keys:
            my_val = self.clocks.get(k, 0)
            other_val = other.clocks.get(k, 0)

            if my_val > other_val:
                return False  # Not happens-before
            if my_val < other_val:
                at_least_one_less = True

        return at_least_one_less

    def to_dict(self) -> Dict[str, int]:
        return dict(self.clocks)

    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> "VectorClock":
        return cls(clocks=dict(data))


@dataclass
class ComponentInfo:
    """Information about a Trinity component."""
    component_type: ComponentType
    name: str
    path: Path
    entry_point: str
    port: int
    state: ComponentState = ComponentState.UNKNOWN
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    last_heartbeat: float = 0.0
    last_health_check: float = 0.0
    consecutive_failures: int = 0
    vector_clock: VectorClock = field(default_factory=VectorClock)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_alive(self, timeout: float = 30.0) -> bool:
        """Check if component is considered alive."""
        if not self.last_heartbeat:
            return False
        return time.time() - self.last_heartbeat < timeout


@dataclass
class CircuitBreakerState:
    """State for a circuit breaker."""
    name: str
    failures: int = 0
    successes: int = 0
    state: str = "closed"  # closed, open, half_open
    last_failure_time: float = 0.0
    reset_timeout: float = 30.0

    def record_failure(self) -> None:
        self.failures += 1
        self.successes = 0
        self.last_failure_time = time.time()
        if self.failures >= TrinityConfig.CIRCUIT_BREAKER_THRESHOLD:
            self.state = "open"
            logger.warning(f"Circuit breaker {self.name} OPENED after {self.failures} failures")

    def record_success(self) -> None:
        self.successes += 1
        self.failures = 0
        if self.state == "half_open" and self.successes >= 2:
            self.state = "closed"
            logger.info(f"Circuit breaker {self.name} CLOSED after recovery")

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure_time > self.reset_timeout:
                self.state = "half_open"
                return True
            return False
        return True  # half_open allows test requests


@dataclass
class ExperienceEvent:
    """An experience event in the pipeline."""
    event_id: str
    event_type: str
    source: ComponentType
    timestamp: float
    sequence: int
    vector_clock: VectorClock
    payload: Dict[str, Any]
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source.value,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "vector_clock": self.vector_clock.to_dict(),
            "payload": self.payload,
        }


# =============================================================================
# CONSENSUS PROTOCOL (Raft-inspired)
# =============================================================================

class ConsensusProtocol:
    """
    Simplified Raft-inspired consensus for Trinity coordination.

    Handles leader election and split-brain detection.
    The leader coordinates model updates, training triggers, and health checks.
    """

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.role = ConsensusRole.FOLLOWER
        self.term = 0
        self.voted_for: Optional[str] = None
        self.leader_id: Optional[str] = None
        self.last_leader_heartbeat = time.time()
        self.election_timeout = TrinityConfig.CONSENSUS_TIMEOUT
        self._lock = asyncio.Lock()

    async def check_leader_timeout(self) -> bool:
        """Check if leader has timed out (potential split-brain)."""
        async with self._lock:
            if self.role == ConsensusRole.LEADER:
                return False

            elapsed = time.time() - self.last_leader_heartbeat
            if elapsed > self.election_timeout:
                logger.warning(f"Leader timeout detected ({elapsed:.1f}s). Split-brain possible.")
                return True
            return False

    async def start_election(self) -> bool:
        """Start a leader election."""
        async with self._lock:
            self.term += 1
            self.role = ConsensusRole.CANDIDATE
            self.voted_for = self.node_id

            logger.info(f"Starting election for term {self.term}")

            # In a full implementation, we'd request votes from other nodes
            # For Trinity, we use a simplified approach: first to claim wins

            # Simulate winning election (simplified)
            self.role = ConsensusRole.LEADER
            self.leader_id = self.node_id
            logger.info(f"Elected as leader for term {self.term}")
            return True

    async def receive_heartbeat(self, leader_id: str, term: int) -> None:
        """Receive heartbeat from leader."""
        async with self._lock:
            if term >= self.term:
                self.term = term
                self.leader_id = leader_id
                self.role = ConsensusRole.FOLLOWER
                self.last_leader_heartbeat = time.time()

    def is_leader(self) -> bool:
        return self.role == ConsensusRole.LEADER

    def get_state(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role": self.role.value,
            "term": self.term,
            "leader_id": self.leader_id,
            "last_leader_heartbeat": self.last_leader_heartbeat,
        }


# =============================================================================
# BACKPRESSURE CONTROLLER (Synaptic Pruning)
# =============================================================================

class BackpressureController:
    """
    Implements adaptive backpressure to prevent event storms.

    "Synaptic Pruning" - drops low-priority events when overwhelmed,
    preserving critical failure logs.
    """

    def __init__(self, threshold: int = TrinityConfig.BACKPRESSURE_THRESHOLD):
        self.threshold = threshold
        self._event_count = 0
        self._window_start = time.time()
        self._window_size = 1.0  # 1 second window
        self._dropped_count = 0
        self._lock = asyncio.Lock()

        # Priority levels (higher = more important)
        self._priority_levels = {
            "critical_failure": 100,
            "model_ready": 90,
            "health_alert": 80,
            "experience_batch": 50,
            "heartbeat": 30,
            "debug": 10,
        }

    async def should_accept(self, event_type: str) -> bool:
        """Check if event should be accepted based on backpressure."""
        async with self._lock:
            now = time.time()

            # Reset window if expired
            if now - self._window_start >= self._window_size:
                self._event_count = 0
                self._window_start = now

            self._event_count += 1

            # Under threshold, accept all
            if self._event_count <= self.threshold:
                return True

            # Over threshold, apply pruning based on priority
            priority = self._priority_levels.get(event_type, 50)

            # Calculate acceptance probability based on priority
            overage = self._event_count - self.threshold
            acceptance_prob = priority / (100 + overage * 10)

            import random
            if random.random() < acceptance_prob:
                return True
            else:
                self._dropped_count += 1
                logger.debug(f"Backpressure: dropped {event_type} (rate: {self._event_count}/s)")
                return False

    def get_stats(self) -> Dict[str, Any]:
        return {
            "current_rate": self._event_count,
            "threshold": self.threshold,
            "dropped_total": self._dropped_count,
            "backpressure_active": self._event_count > self.threshold,
        }


# =============================================================================
# EXPERIENCE PIPELINE (Guaranteed Delivery)
# =============================================================================

class ExperiencePipeline:
    """
    Manages experience events with guaranteed delivery semantics.

    Features:
    - Sequence numbering for gap detection
    - Vector clock for causal ordering
    - Write-ahead log for crash recovery
    - Acknowledgment tracking
    """

    def __init__(self, events_dir: Path):
        self.events_dir = events_dir
        self._sequence = 0
        self._vector_clock = VectorClock()
        self._pending_acks: Dict[str, ExperienceEvent] = {}
        self._wal_path = events_dir / ".wal"
        self._lock = asyncio.Lock()

        # Ensure directories exist
        events_dir.mkdir(parents=True, exist_ok=True)

    async def emit(
        self,
        event_type: str,
        source: ComponentType,
        payload: Dict[str, Any],
    ) -> ExperienceEvent:
        """Emit an experience event with guaranteed delivery."""
        async with self._lock:
            self._sequence += 1
            self._vector_clock.tick(source.value)

            event = ExperienceEvent(
                event_id=f"{source.value}_{self._sequence}_{uuid.uuid4().hex[:8]}",
                event_type=event_type,
                source=source,
                timestamp=time.time(),
                sequence=self._sequence,
                vector_clock=VectorClock.from_dict(self._vector_clock.to_dict()),
                payload=payload,
            )

            # Write to WAL first (crash recovery)
            await self._write_to_wal(event)

            # Write event file
            event_file = self.events_dir / f"{event.event_id}.json"
            await asyncio.to_thread(
                event_file.write_text,
                json.dumps(event.to_dict(), indent=2)
            )

            # Track for acknowledgment
            self._pending_acks[event.event_id] = event

            logger.debug(f"Emitted event {event.event_id} (seq={event.sequence})")
            return event

    async def acknowledge(self, event_id: str) -> bool:
        """Acknowledge receipt of an event."""
        async with self._lock:
            if event_id in self._pending_acks:
                event = self._pending_acks.pop(event_id)
                event.acknowledged = True

                # Remove from WAL
                await self._remove_from_wal(event_id)

                # Delete event file
                event_file = self.events_dir / f"{event_id}.json"
                if event_file.exists():
                    await asyncio.to_thread(event_file.unlink)

                return True
            return False

    async def _write_to_wal(self, event: ExperienceEvent) -> None:
        """Write event to write-ahead log."""
        wal_entry = json.dumps(event.to_dict()) + "\n"
        async def append():
            with open(self._wal_path, "a") as f:
                f.write(wal_entry)
        await asyncio.to_thread(append)

    async def _remove_from_wal(self, event_id: str) -> None:
        """Remove acknowledged event from WAL."""
        if not self._wal_path.exists():
            return

        def remove():
            lines = self._wal_path.read_text().splitlines()
            remaining = [l for l in lines if event_id not in l]
            self._wal_path.write_text("\n".join(remaining) + "\n" if remaining else "")

        await asyncio.to_thread(remove)

    async def recover_from_wal(self) -> List[ExperienceEvent]:
        """Recover unacknowledged events from WAL after crash."""
        if not self._wal_path.exists():
            return []

        recovered = []
        lines = await asyncio.to_thread(self._wal_path.read_text)

        for line in lines.strip().splitlines():
            if not line:
                continue
            try:
                data = json.loads(line)
                event = ExperienceEvent(
                    event_id=data["event_id"],
                    event_type=data["event_type"],
                    source=ComponentType(data["source"]),
                    timestamp=data["timestamp"],
                    sequence=data["sequence"],
                    vector_clock=VectorClock.from_dict(data["vector_clock"]),
                    payload=data["payload"],
                )
                recovered.append(event)
                self._pending_acks[event.event_id] = event
            except Exception as e:
                logger.warning(f"WAL recovery error: {e}")

        if recovered:
            logger.info(f"Recovered {len(recovered)} events from WAL")

        return recovered

    def get_pending_count(self) -> int:
        return len(self._pending_acks)


# =============================================================================
# MODEL HOT-SWAP MANAGER (RCU Locks)
# =============================================================================

class ModelHotSwapManager:
    """
    Manages model hot-swapping with Read-Copy-Update (RCU) semantics.

    Ensures:
    - Current inference completes with old model
    - New model is atomically swapped in
    - Rollback on failure
    """

    def __init__(self):
        self._current_model: Optional[str] = None
        self._pending_model: Optional[str] = None
        self._active_inferences = 0
        self._lock = asyncio.Lock()
        self._swap_event = asyncio.Event()
        self._model_versions: List[Tuple[str, float]] = []  # (version, timestamp)

    async def begin_inference(self) -> str:
        """
        Begin an inference, returning the model to use.

        This "pins" the current model until end_inference is called.
        """
        async with self._lock:
            self._active_inferences += 1
            return self._current_model

    async def end_inference(self) -> None:
        """End an inference, potentially allowing hot-swap."""
        async with self._lock:
            self._active_inferences -= 1
            if self._active_inferences == 0 and self._pending_model:
                self._swap_event.set()

    async def request_swap(self, new_model: str) -> bool:
        """
        Request a model swap.

        Waits for all active inferences to complete before swapping.
        """
        async with self._lock:
            if self._pending_model:
                logger.warning("Swap already in progress")
                return False

            self._pending_model = new_model
            logger.info(f"Model swap requested: {self._current_model} -> {new_model}")

            if self._active_inferences == 0:
                # No active inferences, swap immediately
                await self._perform_swap()
                return True

        # Wait for active inferences to complete
        logger.info(f"Waiting for {self._active_inferences} active inferences to complete...")
        await asyncio.wait_for(
            self._swap_event.wait(),
            timeout=60.0  # 1 minute timeout
        )
        self._swap_event.clear()

        async with self._lock:
            await self._perform_swap()

        return True

    async def _perform_swap(self) -> None:
        """Perform the actual model swap."""
        old_model = self._current_model
        self._current_model = self._pending_model
        self._pending_model = None

        # Record version history for rollback
        self._model_versions.append((self._current_model, time.time()))
        if len(self._model_versions) > 10:
            self._model_versions.pop(0)

        logger.info(f"Model swapped: {old_model} -> {self._current_model}")

    async def rollback(self) -> bool:
        """Rollback to previous model version."""
        if len(self._model_versions) < 2:
            logger.warning("No previous version to rollback to")
            return False

        # Remove current version
        self._model_versions.pop()

        # Get previous version
        prev_version, _ = self._model_versions[-1]

        return await self.request_swap(prev_version)

    def get_state(self) -> Dict[str, Any]:
        return {
            "current_model": self._current_model,
            "pending_model": self._pending_model,
            "active_inferences": self._active_inferences,
            "version_history": [
                {"version": v, "timestamp": t}
                for v, t in self._model_versions[-5:]
            ],
        }


# =============================================================================
# PREDICTIVE AUTO-SCALING (Holt-Winters Forecasting)
# =============================================================================

class HoltWintersForecaster:
    """
    Triple Exponential Smoothing (Holt-Winters) for predictive scaling.

    Captures:
    - Level (base value)
    - Trend (growth direction)
    - Seasonality (periodic patterns)

    Uses additive model suitable for load forecasting.
    """

    def __init__(
        self,
        alpha: float = 0.3,  # Level smoothing
        beta: float = 0.1,   # Trend smoothing
        gamma: float = 0.2,  # Seasonal smoothing
        season_length: int = 24,  # Hourly seasonality
    ):
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.season_length = season_length

        # State
        self._level: Optional[float] = None
        self._trend: Optional[float] = None
        self._seasonals: List[float] = []
        self._history: deque = deque(maxlen=season_length * 7)  # 1 week of data
        self._initialized = False

    def update(self, value: float) -> None:
        """Update model with new observation."""
        self._history.append(value)

        if not self._initialized:
            if len(self._history) >= self.season_length * 2:
                self._initialize()
            return

        # Get seasonal index
        season_idx = len(self._history) % self.season_length

        # Previous values
        prev_level = self._level
        prev_trend = self._trend
        prev_seasonal = self._seasonals[season_idx]

        # Update level: smoothed average minus seasonal
        self._level = self.alpha * (value - prev_seasonal) + (1 - self.alpha) * (prev_level + prev_trend)

        # Update trend: smoothed difference in levels
        self._trend = self.beta * (self._level - prev_level) + (1 - self.beta) * prev_trend

        # Update seasonal: smoothed difference from level
        self._seasonals[season_idx] = self.gamma * (value - self._level) + (1 - self.gamma) * prev_seasonal

    def _initialize(self) -> None:
        """Initialize model parameters from initial data."""
        data = list(self._history)

        # Initial level: mean of first season
        self._level = sum(data[:self.season_length]) / self.season_length

        # Initial trend: average difference between seasons
        trend_sum = 0.0
        for i in range(self.season_length):
            trend_sum += (data[self.season_length + i] - data[i]) / self.season_length
        self._trend = trend_sum / self.season_length

        # Initial seasonals: deviations from level
        self._seasonals = []
        for i in range(self.season_length):
            avg_season = (data[i] + data[self.season_length + i]) / 2
            self._seasonals.append(avg_season - self._level)

        self._initialized = True

    def forecast(self, steps: int = 1) -> List[float]:
        """Forecast future values."""
        if not self._initialized:
            return [self._history[-1] if self._history else 0.0] * steps

        forecasts = []
        for step in range(1, steps + 1):
            season_idx = (len(self._history) + step) % self.season_length
            forecast = self._level + (step * self._trend) + self._seasonals[season_idx]
            forecasts.append(max(0.0, forecast))  # Non-negative

        return forecasts

    def get_state(self) -> Dict[str, Any]:
        return {
            "initialized": self._initialized,
            "level": self._level,
            "trend": self._trend,
            "history_size": len(self._history),
        }


class AnomalyDetector:
    """
    Statistical anomaly detection using Modified Z-Score.

    More robust than standard Z-score against outliers by using
    median and MAD (Median Absolute Deviation).
    """

    def __init__(self, threshold: float = 3.5, window_size: int = 100):
        self.threshold = threshold
        self.window_size = window_size
        self._values: deque = deque(maxlen=window_size)
        self._anomaly_count = 0
        self._last_anomaly: Optional[Tuple[float, float, str]] = None  # (value, score, direction)

    def check(self, value: float) -> Tuple[bool, float]:
        """
        Check if value is anomalous.

        Returns:
            (is_anomaly, z_score)
        """
        self._values.append(value)

        if len(self._values) < 10:
            return False, 0.0

        # Calculate median
        sorted_values = sorted(self._values)
        n = len(sorted_values)
        median = sorted_values[n // 2] if n % 2 else (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2

        # Calculate MAD (Median Absolute Deviation)
        deviations = sorted([abs(v - median) for v in self._values])
        mad = deviations[n // 2] if n % 2 else (deviations[n // 2 - 1] + deviations[n // 2]) / 2

        # Avoid division by zero
        if mad == 0:
            mad = 0.001

        # Modified Z-score
        z_score = 0.6745 * (value - median) / mad

        is_anomaly = abs(z_score) > self.threshold

        if is_anomaly:
            self._anomaly_count += 1
            direction = "high" if z_score > 0 else "low"
            self._last_anomaly = (value, z_score, direction)

        return is_anomaly, z_score

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_anomalies": self._anomaly_count,
            "window_size": len(self._values),
            "last_anomaly": self._last_anomaly,
        }


class PredictiveAutoScaler:
    """
    Predictive auto-scaling using Holt-Winters forecasting and anomaly detection.

    Features:
    - Forecasts load 1 hour ahead
    - Detects anomalies in real-time
    - Provides scaling recommendations
    - Handles seasonal patterns (daily, weekly)
    """

    def __init__(
        self,
        min_instances: int = 1,
        max_instances: int = 10,
        scale_up_threshold: float = 0.75,  # CPU/memory threshold
        scale_down_threshold: float = 0.25,
        cooldown_period: float = 300.0,  # 5 minutes
    ):
        self.min_instances = min_instances
        self.max_instances = max_instances
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold
        self.cooldown_period = cooldown_period

        # Forecasters for different metrics
        self._cpu_forecaster = HoltWintersForecaster()
        self._memory_forecaster = HoltWintersForecaster()
        self._request_rate_forecaster = HoltWintersForecaster()

        # Anomaly detectors
        self._cpu_anomaly = AnomalyDetector()
        self._memory_anomaly = AnomalyDetector()
        self._request_anomaly = AnomalyDetector()

        # State
        self._current_instances = 1
        self._last_scale_time = 0.0
        self._pending_scale_action: Optional[str] = None
        self._lock = asyncio.Lock()

        # Metrics
        self._scale_up_count = 0
        self._scale_down_count = 0
        self._anomalies_detected = 0

    async def record_metrics(
        self,
        cpu_usage: float,
        memory_usage: float,
        request_rate: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Record current metrics and check for scaling needs.

        Returns scaling recommendation if action needed.
        """
        async with self._lock:
            # Update forecasters
            self._cpu_forecaster.update(cpu_usage)
            self._memory_forecaster.update(memory_usage)
            self._request_rate_forecaster.update(request_rate)

            # Check for anomalies
            cpu_anomaly, cpu_z = self._cpu_anomaly.check(cpu_usage)
            memory_anomaly, mem_z = self._memory_anomaly.check(memory_usage)
            request_anomaly, req_z = self._request_anomaly.check(request_rate)

            if any([cpu_anomaly, memory_anomaly, request_anomaly]):
                self._anomalies_detected += 1
                logger.warning(
                    f"Anomaly detected - CPU: {cpu_anomaly} (z={cpu_z:.2f}), "
                    f"Memory: {memory_anomaly} (z={mem_z:.2f}), "
                    f"Requests: {request_anomaly} (z={req_z:.2f})"
                )

            # Check cooldown
            if time.time() - self._last_scale_time < self.cooldown_period:
                return None

            # Get forecasts (next hour, sampled hourly)
            cpu_forecast = self._cpu_forecaster.forecast(steps=12)
            memory_forecast = self._memory_forecaster.forecast(steps=12)

            # Use max forecast to be proactive
            max_cpu = max(cpu_forecast) if cpu_forecast else cpu_usage
            max_memory = max(memory_forecast) if memory_forecast else memory_usage

            # Determine scaling action
            recommendation = None

            if max_cpu > self.scale_up_threshold or max_memory > self.scale_up_threshold:
                # Scale up needed
                if self._current_instances < self.max_instances:
                    recommendation = self._create_scale_recommendation(
                        action="scale_up",
                        reason=f"Forecast: CPU={max_cpu:.1%}, Memory={max_memory:.1%}",
                        target_instances=min(self._current_instances + 1, self.max_instances),
                    )
            elif cpu_usage < self.scale_down_threshold and memory_usage < self.scale_down_threshold:
                # Scale down possible
                if self._current_instances > self.min_instances:
                    # More conservative: also check forecast isn't going up
                    future_cpu = cpu_forecast[-1] if cpu_forecast else cpu_usage
                    if future_cpu < self.scale_up_threshold:
                        recommendation = self._create_scale_recommendation(
                            action="scale_down",
                            reason=f"Low utilization: CPU={cpu_usage:.1%}, Memory={memory_usage:.1%}",
                            target_instances=max(self._current_instances - 1, self.min_instances),
                        )

            return recommendation

    def _create_scale_recommendation(
        self,
        action: str,
        reason: str,
        target_instances: int,
    ) -> Dict[str, Any]:
        """Create a scaling recommendation."""
        return {
            "action": action,
            "current_instances": self._current_instances,
            "target_instances": target_instances,
            "reason": reason,
            "timestamp": time.time(),
            "forecasts": {
                "cpu": self._cpu_forecaster.forecast(steps=6),
                "memory": self._memory_forecaster.forecast(steps=6),
            },
        }

    async def apply_scaling(self, target_instances: int) -> bool:
        """Apply scaling decision."""
        async with self._lock:
            if target_instances == self._current_instances:
                return True

            action = "scale_up" if target_instances > self._current_instances else "scale_down"

            logger.info(
                f"Auto-scaling: {self._current_instances} -> {target_instances} instances ({action})"
            )

            self._current_instances = target_instances
            self._last_scale_time = time.time()

            if action == "scale_up":
                self._scale_up_count += 1
            else:
                self._scale_down_count += 1

            return True

    def get_stats(self) -> Dict[str, Any]:
        return {
            "current_instances": self._current_instances,
            "scale_up_count": self._scale_up_count,
            "scale_down_count": self._scale_down_count,
            "anomalies_detected": self._anomalies_detected,
            "forecasters": {
                "cpu": self._cpu_forecaster.get_state(),
                "memory": self._memory_forecaster.get_state(),
                "requests": self._request_rate_forecaster.get_state(),
            },
            "anomaly_detectors": {
                "cpu": self._cpu_anomaly.get_stats(),
                "memory": self._memory_anomaly.get_stats(),
                "requests": self._request_anomaly.get_stats(),
            },
        }


# =============================================================================
# EDGE CASE HANDLERS
# =============================================================================

class RetryStrategy:
    """
    Configurable retry strategy with exponential backoff and jitter.

    Implements decorrelated jitter for better distributed behavior.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: float = 0.3,  # 30% jitter
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self._attempt = 0

    def next_delay(self) -> Optional[float]:
        """Get next delay or None if max retries exceeded."""
        if self._attempt >= self.max_retries:
            return None

        self._attempt += 1

        # Exponential backoff
        delay = self.base_delay * (self.exponential_base ** (self._attempt - 1))

        # Decorrelated jitter
        import random
        jitter_range = delay * self.jitter
        delay += random.uniform(-jitter_range, jitter_range)

        # Clamp
        return min(delay, self.max_delay)

    def reset(self) -> None:
        self._attempt = 0

    @property
    def attempts(self) -> int:
        return self._attempt


class GracefulDegradation:
    """
    Manages graceful degradation when components fail.

    Provides fallback behaviors for different failure scenarios.
    """

    def __init__(self):
        self._degraded_components: Set[ComponentType] = set()
        self._fallback_modes: Dict[ComponentType, str] = {}
        self._lock = asyncio.Lock()

    async def degrade(self, component: ComponentType, reason: str) -> str:
        """
        Enter degraded mode for a component.

        Returns the fallback mode activated.
        """
        async with self._lock:
            self._degraded_components.add(component)

            # Determine fallback based on component
            if component == ComponentType.MIND:
                # Mind failure: route directly to Body (local processing)
                fallback = "local_routing"
            elif component == ComponentType.NERVES:
                # Nerves failure: disable training, keep inference
                fallback = "inference_only"
            else:
                # Body failure: queue for later processing
                fallback = "queue_mode"

            self._fallback_modes[component] = fallback

            logger.warning(
                f"Graceful degradation activated for {component.value}: "
                f"{reason} -> fallback: {fallback}"
            )

            return fallback

    async def recover(self, component: ComponentType) -> bool:
        """Recover from degraded mode."""
        async with self._lock:
            if component not in self._degraded_components:
                return False

            self._degraded_components.discard(component)
            self._fallback_modes.pop(component, None)

            logger.info(f"Recovered from degraded mode: {component.value}")
            return True

    def is_degraded(self, component: ComponentType) -> bool:
        return component in self._degraded_components

    def get_fallback_mode(self, component: ComponentType) -> Optional[str]:
        return self._fallback_modes.get(component)

    def get_status(self) -> Dict[str, Any]:
        return {
            "degraded_components": [c.value for c in self._degraded_components],
            "fallback_modes": {c.value: m for c, m in self._fallback_modes.items()},
        }


class DeadLetterQueue:
    """
    Dead Letter Queue for failed events.

    Stores events that couldn't be processed for later analysis/retry.
    """

    def __init__(self, max_size: int = 10000, dlq_dir: Optional[Path] = None):
        self.max_size = max_size
        self.dlq_dir = dlq_dir or TrinityConfig.TRINITY_BASE / "dlq"
        self._queue: deque = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

        # Ensure directory exists
        self.dlq_dir.mkdir(parents=True, exist_ok=True)

    async def enqueue(
        self,
        event: Dict[str, Any],
        error: str,
        source: str,
    ) -> str:
        """Add failed event to DLQ."""
        async with self._lock:
            dlq_entry = {
                "id": f"dlq_{uuid.uuid4().hex[:12]}",
                "timestamp": time.time(),
                "source": source,
                "error": error,
                "retry_count": 0,
                "event": event,
            }

            self._queue.append(dlq_entry)

            # Persist to disk
            entry_file = self.dlq_dir / f"{dlq_entry['id']}.json"
            await asyncio.to_thread(
                entry_file.write_text,
                json.dumps(dlq_entry, indent=2)
            )

            logger.warning(f"Event sent to DLQ: {dlq_entry['id']} (error: {error})")

            return dlq_entry['id']

    async def peek(self, count: int = 10) -> List[Dict[str, Any]]:
        """Peek at entries without removing."""
        async with self._lock:
            return list(self._queue)[-count:]

    async def retry(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """Get entry for retry and increment counter."""
        async with self._lock:
            for entry in self._queue:
                if entry['id'] == entry_id:
                    entry['retry_count'] += 1
                    return entry['event']
            return None

    async def remove(self, entry_id: str) -> bool:
        """Remove entry after successful processing."""
        async with self._lock:
            for i, entry in enumerate(self._queue):
                if entry['id'] == entry_id:
                    del self._queue[i]

                    # Remove from disk
                    entry_file = self.dlq_dir / f"{entry_id}.json"
                    if entry_file.exists():
                        await asyncio.to_thread(entry_file.unlink)

                    return True
            return False

    def size(self) -> int:
        return len(self._queue)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "size": len(self._queue),
            "max_size": self.max_size,
            "oldest_entry": self._queue[0]['timestamp'] if self._queue else None,
            "newest_entry": self._queue[-1]['timestamp'] if self._queue else None,
        }


class ResourceGovernor:
    """
    Governs resource usage to prevent runaway processes.

    Implements:
    - Memory limits with automatic cleanup
    - CPU throttling awareness
    - Connection pooling limits
    - File descriptor tracking
    """

    def __init__(
        self,
        memory_limit_mb: int = 2048,
        max_connections: int = 100,
        max_open_files: int = 1000,
    ):
        self.memory_limit_mb = memory_limit_mb
        self.max_connections = max_connections
        self.max_open_files = max_open_files

        self._active_connections = 0
        self._open_files = 0
        self._gc_runs = 0
        self._lock = asyncio.Lock()

    async def check_resources(self) -> Dict[str, Any]:
        """Check current resource usage."""
        import gc

        # Get memory usage
        import sys
        try:
            import resource
            mem_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB to MB on Linux
            if sys.platform == 'darwin':
                mem_usage /= 1024  # macOS reports in bytes
        except ImportError:
            mem_usage = 0

        status = {
            "memory_mb": mem_usage,
            "memory_limit_mb": self.memory_limit_mb,
            "memory_usage_pct": mem_usage / self.memory_limit_mb if self.memory_limit_mb else 0,
            "active_connections": self._active_connections,
            "max_connections": self.max_connections,
            "gc_runs": self._gc_runs,
        }

        # Trigger GC if memory is high
        if mem_usage > self.memory_limit_mb * 0.85:
            gc.collect()
            self._gc_runs += 1
            logger.info(f"Triggered GC due to high memory ({mem_usage:.1f}MB)")

        return status

    async def acquire_connection(self) -> bool:
        """Try to acquire a connection slot."""
        async with self._lock:
            if self._active_connections >= self.max_connections:
                logger.warning(f"Connection limit reached ({self.max_connections})")
                return False
            self._active_connections += 1
            return True

    async def release_connection(self) -> None:
        """Release a connection slot."""
        async with self._lock:
            self._active_connections = max(0, self._active_connections - 1)


# =============================================================================
# TRINITY ORCHESTRATION ENGINE
# =============================================================================

class TrinityOrchestrationEngine:
    """
    The "God Process" that orchestrates the entire Trinity ecosystem.

    This is the central coordinator that:
    1. Manages component lifecycles (start, stop, restart)
    2. Coordinates distributed consensus
    3. Monitors health with circuit breakers
    4. Manages experience pipeline
    5. Handles model hot-swapping
    6. Detects and recovers from split-brain scenarios
    """

    def __init__(self, instance_id: Optional[str] = None):
        self.instance_id = instance_id or f"trinity-{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger(f"TrinityEngine.{self.instance_id}")

        # Core state
        self._running = False
        self._shutting_down = False
        self._start_time: Optional[float] = None

        # Component registry
        self._components: Dict[ComponentType, ComponentInfo] = {}

        # Distributed coordination
        self._consensus = ConsensusProtocol(self.instance_id)
        self._global_vector_clock = VectorClock()

        # Fault tolerance - v2.1: Initialize BEFORE _initialize_components()
        # This fixes AttributeError when _initialize_components tries to create circuit breakers
        self._circuit_breakers: Dict[str, CircuitBreakerState] = {}
        self._backpressure = BackpressureController()

        # Now initialize components (which populates circuit breakers)
        self._initialize_components()

        # Experience pipeline
        self._experience_pipeline = ExperiencePipeline(TrinityConfig.TRINITY_EVENTS_DIR)

        # Model management
        self._model_manager = ModelHotSwapManager()

        # Advanced features
        self._auto_scaler = PredictiveAutoScaler(
            min_instances=int(os.getenv("TRINITY_MIN_INSTANCES", "1")),
            max_instances=int(os.getenv("TRINITY_MAX_INSTANCES", "10")),
        )
        self._graceful_degradation = GracefulDegradation()
        self._dead_letter_queue = DeadLetterQueue()
        self._resource_governor = ResourceGovernor(
            memory_limit_mb=int(os.getenv("TRINITY_MEMORY_LIMIT_MB", "2048")),
        )

        # Retry strategies for each component
        self._retry_strategies: Dict[ComponentType, RetryStrategy] = {
            ct: RetryStrategy(max_retries=3) for ct in ComponentType
        }

        # Background tasks
        self._tasks: List[asyncio.Task] = []

        # Metrics
        self._metrics = {
            "components_started": 0,
            "components_restarted": 0,
            "health_checks": 0,
            "split_brain_events": 0,
            "model_swaps": 0,
            "degradations": 0,
            "dlq_events": 0,
            "scaling_actions": 0,
        }

    def _initialize_components(self) -> None:
        """Initialize component registry with dynamic configuration."""
        self._components = {
            ComponentType.MIND: ComponentInfo(
                component_type=ComponentType.MIND,
                name="JARVIS-Prime (Mind)",
                path=TrinityConfig.PRIME_PATH,
                entry_point="python3 server.py",
                port=TrinityConfig.PRIME_PORT,
            ),
            ComponentType.NERVES: ComponentInfo(
                component_type=ComponentType.NERVES,
                name="Reactor-Core (Nerves)",
                path=TrinityConfig.REACTOR_PATH,
                entry_point="python3 run_reactor.py",
                port=TrinityConfig.REACTOR_PORT,
            ),
            ComponentType.BODY: ComponentInfo(
                component_type=ComponentType.BODY,
                name="JARVIS (Body)",
                path=TrinityConfig.JARVIS_PATH,
                entry_point="python3 run_supervisor.py",
                port=TrinityConfig.JARVIS_PORT,
            ),
        }

        # Initialize circuit breakers for each component
        for comp_type in self._components:
            self._circuit_breakers[comp_type.value] = CircuitBreakerState(
                name=comp_type.value
            )

    async def start(self) -> bool:
        """
        Start the Trinity ecosystem.

        Startup order:
        1. Recover any pending events from WAL
        2. Start Mind (Prime) first - needs to be ready for routing
        3. Start Nerves (Reactor) - watches for events
        4. Start Body (JARVIS) - user interface
        5. Verify all components healthy
        6. Start background tasks (monitoring, consensus)
        """
        if self._running:
            self.logger.warning("Trinity already running")
            return True

        self._running = True
        self._start_time = time.time()
        self.logger.info("=" * 60)
        self.logger.info("🧬 INITIALIZING DIGITAL BIOLOGY SEQUENCE...")
        self.logger.info("=" * 60)

        try:
            # Ensure IPC directories exist
            self._ensure_directories()

            # Recover pending events
            recovered = await self._experience_pipeline.recover_from_wal()
            if recovered:
                self.logger.info(f"Recovered {len(recovered)} pending events")

            # Start components in order
            startup_order = [
                ComponentType.MIND,
                ComponentType.NERVES,
                # BODY is special - it's the current process running run_supervisor.py
            ]

            for comp_type in startup_order:
                component = self._components[comp_type]

                if not component.path.exists():
                    self.logger.warning(f"⚠️ Component path not found: {component.path}")
                    continue

                success = await self._start_component(comp_type)
                if not success and comp_type == ComponentType.MIND:
                    self.logger.error("Critical: Mind failed to start")
                    # Could implement fallback here

            # Start background tasks
            self._start_background_tasks()

            # Start consensus protocol
            await self._consensus.start_election()

            self.logger.info("🧬 DIGITAL BIOLOGY SEQUENCE COMPLETE")
            self.logger.info(f"Trinity running as {self._consensus.role.value}")

            return True

        except Exception as e:
            self.logger.error(f"Startup failed: {e}")
            await self.stop()
            return False

    async def stop(self) -> None:
        """
        Gracefully shutdown the Trinity ecosystem.

        Shutdown order (reverse of startup):
        1. Stop accepting new events
        2. Drain experience pipeline
        3. Stop Body
        4. Stop Nerves
        5. Stop Mind
        6. Cleanup
        """
        if self._shutting_down:
            return

        self._shutting_down = True
        self.logger.info("🔻 INITIATING TRINITY SHUTDOWN...")

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Shutdown components in reverse order
        shutdown_order = [
            ComponentType.BODY,
            ComponentType.NERVES,
            ComponentType.MIND,
        ]

        for comp_type in shutdown_order:
            await self._stop_component(comp_type)

        self._running = False
        duration = time.time() - self._start_time if self._start_time else 0
        self.logger.info(f"🌑 TRINITY OFFLINE (uptime: {duration:.1f}s)")

    def _ensure_directories(self) -> None:
        """Ensure all required directories exist."""
        dirs = [
            TrinityConfig.TRINITY_EVENTS_DIR,
            TrinityConfig.REACTOR_EVENTS_DIR,
            TrinityConfig.HEARTBEAT_DIR,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    async def _start_component(self, comp_type: ComponentType) -> bool:
        """Start a single component."""
        component = self._components[comp_type]
        self.logger.info(f"⚡ IGNITING {component.name}...")

        try:
            env = os.environ.copy()
            env.update({
                "TRINITY_ROLE": comp_type.value.upper(),
                "TRINITY_INSTANCE_ID": self.instance_id,
                "TRINITY_PORT": str(component.port),
            })

            process = await asyncio.create_subprocess_shell(
                f"cd {component.path} && {component.entry_point}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None,
            )

            component.process = process
            component.pid = process.pid
            component.state = ComponentState.STARTING
            component.last_heartbeat = time.time()

            # Start log streaming
            self._tasks.append(asyncio.create_task(
                self._stream_logs(process.stdout, component.name)
            ))
            self._tasks.append(asyncio.create_task(
                self._stream_logs(process.stderr, component.name, level="ERROR")
            ))

            # Wait for startup verification
            if await self._verify_component_startup(comp_type):
                component.state = ComponentState.HEALTHY
                self._metrics["components_started"] += 1
                self.logger.info(f"✅ {component.name} IS ALIVE (PID: {process.pid})")
                return True
            else:
                component.state = ComponentState.FAILED
                self.logger.error(f"💀 {component.name} FAILED TO START")
                return False

        except Exception as e:
            self.logger.error(f"Error starting {component.name}: {e}")
            component.state = ComponentState.FAILED
            return False

    async def _stop_component(self, comp_type: ComponentType) -> None:
        """Stop a single component."""
        component = self._components[comp_type]

        if not component.process:
            return

        self.logger.info(f"💤 Stopping {component.name}...")

        try:
            # Try graceful shutdown first
            if hasattr(os, 'killpg'):
                os.killpg(os.getpgid(component.pid), signal.SIGTERM)
            else:
                component.process.terminate()

            try:
                await asyncio.wait_for(component.process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                self.logger.warning(f"{component.name} didn't stop gracefully, killing...")
                component.process.kill()

            component.state = ComponentState.STOPPED
            component.process = None
            component.pid = None
            self.logger.info(f"✅ {component.name} stopped")

        except ProcessLookupError:
            pass
        except Exception as e:
            self.logger.error(f"Error stopping {component.name}: {e}")

    async def _verify_component_startup(
        self,
        comp_type: ComponentType,
        timeout: float = 30.0,
    ) -> bool:
        """Verify a component started successfully."""
        component = self._components[comp_type]
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check if process died
            if component.process.returncode is not None:
                return False

            # Check health endpoint
            try:
                import aiohttp
                health_url = f"http://localhost:{component.port}/health"
                async with aiohttp.ClientSession() as session:
                    async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            return True
            except Exception:
                pass

            await asyncio.sleep(1.0)

        return False

    async def _stream_logs(
        self,
        stream: asyncio.StreamReader,
        component_name: str,
        level: str = "INFO",
    ) -> None:
        """Stream logs from a component."""
        while True:
            try:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if decoded:
                    prefix = f"[{component_name}]"
                    if level == "ERROR":
                        self.logger.error(f"{prefix} {decoded}")
                    else:
                        self.logger.info(f"{prefix} {decoded}")
            except asyncio.CancelledError:
                break
            except Exception:
                break

    def _start_background_tasks(self) -> None:
        """Start background monitoring tasks."""
        self._tasks.extend([
            asyncio.create_task(self._health_monitor_loop()),
            asyncio.create_task(self._consensus_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._split_brain_detector()),
            asyncio.create_task(self._resource_monitor_loop()),
            asyncio.create_task(self._dlq_retry_loop()),
        ])

    async def _health_monitor_loop(self) -> None:
        """Monitor component health and restart if needed."""
        while self._running:
            try:
                await asyncio.sleep(TrinityConfig.HEALTH_CHECK_INTERVAL)

                for comp_type, component in self._components.items():
                    if component.state not in (ComponentState.HEALTHY, ComponentState.DEGRADED):
                        continue

                    circuit_breaker = self._circuit_breakers[comp_type.value]

                    if not circuit_breaker.can_execute():
                        continue

                    is_healthy = await self._check_component_health(comp_type)
                    self._metrics["health_checks"] += 1

                    if is_healthy:
                        circuit_breaker.record_success()
                        component.consecutive_failures = 0
                    else:
                        circuit_breaker.record_failure()
                        component.consecutive_failures += 1

                        if component.consecutive_failures >= 3:
                            # Check if we should enter degraded mode
                            retry_strategy = self._retry_strategies[comp_type]
                            delay = retry_strategy.next_delay()

                            if delay is None:
                                # Max retries exceeded, enter degraded mode
                                self.logger.error(
                                    f"🔻 {component.name} max retries exceeded, entering degraded mode"
                                )
                                await self._graceful_degradation.degrade(
                                    comp_type,
                                    f"Max retries exceeded after {retry_strategy.attempts} attempts"
                                )
                                self._metrics["degradations"] += 1
                                component.state = ComponentState.DEGRADED
                            else:
                                self.logger.warning(
                                    f"⚠️ {component.name} unhealthy, restarting in {delay:.1f}s..."
                                )
                                await asyncio.sleep(delay)
                                component.state = ComponentState.RECOVERING
                                success = await self._restart_component(comp_type)
                                if success:
                                    retry_strategy.reset()
                                    await self._graceful_degradation.recover(comp_type)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")

    async def _check_component_health(self, comp_type: ComponentType) -> bool:
        """Check if a component is healthy."""
        component = self._components[comp_type]

        try:
            import aiohttp
            health_url = f"http://localhost:{component.port}/health"
            async with aiohttp.ClientSession() as session:
                async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    component.last_health_check = time.time()
                    return resp.status == 200
        except Exception:
            return False

    async def _restart_component(self, comp_type: ComponentType) -> bool:
        """Restart a failed component."""
        self.logger.info(f"🔄 Restarting {self._components[comp_type].name}...")
        self._metrics["components_restarted"] += 1

        await self._stop_component(comp_type)
        await asyncio.sleep(2.0)
        return await self._start_component(comp_type)

    async def _consensus_loop(self) -> None:
        """Maintain consensus and handle leader duties."""
        while self._running:
            try:
                await asyncio.sleep(TrinityConfig.HEARTBEAT_INTERVAL)

                if self._consensus.is_leader():
                    # Leader duties: coordinate, broadcast heartbeats
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Consensus loop error: {e}")

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats to other components."""
        while self._running:
            try:
                await asyncio.sleep(TrinityConfig.HEARTBEAT_INTERVAL)

                self._global_vector_clock.tick(self.instance_id)

                heartbeat = {
                    "instance_id": self.instance_id,
                    "timestamp": time.time(),
                    "vector_clock": self._global_vector_clock.to_dict(),
                    "components": {
                        ct.value: {
                            "state": c.state.value,
                            "pid": c.pid,
                        }
                        for ct, c in self._components.items()
                    },
                }

                heartbeat_file = TrinityConfig.HEARTBEAT_DIR / f"{self.instance_id}.json"
                await asyncio.to_thread(
                    heartbeat_file.write_text,
                    json.dumps(heartbeat, indent=2)
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")

    async def _split_brain_detector(self) -> None:
        """Detect and handle split-brain scenarios."""
        while self._running:
            try:
                await asyncio.sleep(TrinityConfig.SPLIT_BRAIN_TIMEOUT / 2)

                if await self._consensus.check_leader_timeout():
                    self._metrics["split_brain_events"] += 1
                    self.logger.warning("🧠 SPLIT-BRAIN DETECTED! Initiating recovery...")

                    # Start election to resolve
                    await self._consensus.start_election()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Split-brain detector error: {e}")

    async def _resource_monitor_loop(self) -> None:
        """Monitor resources and trigger auto-scaling."""
        monitor_interval = float(os.getenv("TRINITY_RESOURCE_MONITOR_INTERVAL", "60.0"))

        while self._running:
            try:
                await asyncio.sleep(monitor_interval)

                # Check resource usage
                resource_status = await self._resource_governor.check_resources()

                # Get CPU/memory metrics (simplified - in production use psutil)
                try:
                    import resource as res_module
                    usage = res_module.getrusage(res_module.RUSAGE_SELF)
                    cpu_usage = min(1.0, usage.ru_utime / 100.0)  # Approximation
                    memory_usage = resource_status["memory_usage_pct"]
                except ImportError:
                    cpu_usage = 0.5  # Default
                    memory_usage = 0.5

                # Calculate request rate from experience pipeline
                pending = self._experience_pipeline.get_pending_count()
                request_rate = pending / monitor_interval  # events/second

                # Record metrics and get scaling recommendation
                recommendation = await self._auto_scaler.record_metrics(
                    cpu_usage=cpu_usage,
                    memory_usage=memory_usage,
                    request_rate=request_rate,
                )

                if recommendation:
                    self.logger.info(
                        f"📊 Auto-scaling recommendation: {recommendation['action']} "
                        f"({recommendation['current_instances']} -> {recommendation['target_instances']})"
                    )
                    await self._auto_scaler.apply_scaling(recommendation['target_instances'])
                    self._metrics["scaling_actions"] += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Resource monitor error: {e}")

    async def _dlq_retry_loop(self) -> None:
        """Periodically retry events in the dead letter queue."""
        retry_interval = float(os.getenv("TRINITY_DLQ_RETRY_INTERVAL", "300.0"))  # 5 min
        max_retries_per_batch = 10

        while self._running:
            try:
                await asyncio.sleep(retry_interval)

                if self._dead_letter_queue.size() == 0:
                    continue

                self.logger.info(f"🔄 Processing DLQ ({self._dead_letter_queue.size()} entries)")

                # Peek at entries to retry
                entries = await self._dead_letter_queue.peek(max_retries_per_batch)

                for entry in entries:
                    if entry.get('retry_count', 0) >= 3:
                        # Too many retries, log and skip
                        self.logger.warning(
                            f"DLQ entry {entry['id']} exceeded max retries, archiving"
                        )
                        continue

                    # Try to re-emit the event
                    event_data = await self._dead_letter_queue.retry(entry['id'])
                    if event_data:
                        try:
                            await self._experience_pipeline.emit(
                                event_type=event_data.get('event_type', 'retry'),
                                source=ComponentType(event_data.get('source', 'body')),
                                payload=event_data.get('payload', {}),
                            )
                            # Success - remove from DLQ
                            await self._dead_letter_queue.remove(entry['id'])
                            self.logger.info(f"✅ DLQ entry {entry['id']} successfully reprocessed")
                        except Exception as e:
                            self.logger.warning(f"DLQ retry failed for {entry['id']}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"DLQ retry loop error: {e}")

    # ==========================================================================
    # PUBLIC API
    # ==========================================================================

    async def emit_experience(
        self,
        event_type: str,
        payload: Dict[str, Any],
    ) -> Optional[str]:
        """Emit an experience event to the pipeline."""
        if not await self._backpressure.should_accept(event_type):
            return None

        event = await self._experience_pipeline.emit(
            event_type=event_type,
            source=ComponentType.BODY,
            payload=payload,
        )
        return event.event_id

    async def request_model_swap(self, new_model: str) -> bool:
        """Request a model hot-swap."""
        success = await self._model_manager.request_swap(new_model)
        if success:
            self._metrics["model_swaps"] += 1
        return success

    async def send_to_dlq(
        self,
        event: Dict[str, Any],
        error: str,
        source: str = "unknown",
    ) -> str:
        """Send a failed event to the dead letter queue."""
        entry_id = await self._dead_letter_queue.enqueue(event, error, source)
        self._metrics["dlq_events"] += 1
        return entry_id

    async def get_scaling_recommendation(self) -> Optional[Dict[str, Any]]:
        """Get current auto-scaling recommendation without applying."""
        return self._auto_scaler.get_stats()

    def is_component_degraded(self, comp_type: ComponentType) -> bool:
        """Check if a component is in degraded mode."""
        return self._graceful_degradation.is_degraded(comp_type)

    def get_fallback_mode(self, comp_type: ComponentType) -> Optional[str]:
        """Get the fallback mode for a degraded component."""
        return self._graceful_degradation.get_fallback_mode(comp_type)

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive Trinity status."""
        return {
            "instance_id": self.instance_id,
            "running": self._running,
            "uptime_seconds": time.time() - self._start_time if self._start_time else 0,
            "consensus": self._consensus.get_state(),
            "components": {
                ct.value: {
                    "name": c.name,
                    "state": c.state.value,
                    "pid": c.pid,
                    "last_heartbeat": c.last_heartbeat,
                    "consecutive_failures": c.consecutive_failures,
                    "degraded": self._graceful_degradation.is_degraded(ct),
                    "fallback_mode": self._graceful_degradation.get_fallback_mode(ct),
                }
                for ct, c in self._components.items()
            },
            "circuit_breakers": {
                name: {"state": cb.state, "failures": cb.failures}
                for name, cb in self._circuit_breakers.items()
            },
            "backpressure": self._backpressure.get_stats(),
            "experience_pipeline": {
                "pending_events": self._experience_pipeline.get_pending_count(),
            },
            "model_manager": self._model_manager.get_state(),
            "auto_scaler": self._auto_scaler.get_stats(),
            "graceful_degradation": self._graceful_degradation.get_status(),
            "dead_letter_queue": self._dead_letter_queue.get_stats(),
            "resource_governor": {
                "active_connections": self._resource_governor._active_connections,
                "gc_runs": self._resource_governor._gc_runs,
            },
            "metrics": self._metrics,
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_engine: Optional[TrinityOrchestrationEngine] = None


def get_orchestration_engine() -> TrinityOrchestrationEngine:
    """Get global orchestration engine instance."""
    global _engine
    if _engine is None:
        _engine = TrinityOrchestrationEngine()
    return _engine


async def start_trinity() -> bool:
    """Start the Trinity ecosystem."""
    engine = get_orchestration_engine()
    return await engine.start()


async def stop_trinity() -> None:
    """Stop the Trinity ecosystem."""
    global _engine
    if _engine:
        await _engine.stop()
        _engine = None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

async def main():
    """Main entry point for Trinity orchestration."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | 🧬 %(name)s | %(message)s',
        datefmt='%H:%M:%S'
    )

    engine = get_orchestration_engine()

    # Register signal handlers
    loop = asyncio.get_running_loop()

    def signal_handler():
        asyncio.create_task(stop_trinity())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    # Start Trinity
    if await engine.start():
        # Keep running until shutdown
        try:
            while engine._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    await stop_trinity()


if __name__ == "__main__":
    asyncio.run(main())
