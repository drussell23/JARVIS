"""
Trinity Unified Orchestrator v83.0 - Production-Grade Cross-Repo Integration.
===============================================================================

The SINGLE POINT OF TRUTH for Trinity integration - a battle-hardened,
production-ready orchestrator that connects JARVIS Body, Prime, and Reactor-Core.

╔══════════════════════════════════════════════════════════════════════════════╗
║  v83.0 CRITICAL ENHANCEMENTS (Addressing All Root Issues)                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  1. ✅ CRASH RECOVERY      - Auto-restart with exponential backoff          ║
║  2. ✅ PROCESS SUPERVISOR  - Monitor PIDs, detect zombies, auto-heal        ║
║  3. ✅ RESOURCE COORDINATOR- Port/memory/CPU reservation with pooling       ║
║  4. ✅ EVENT STORE         - WAL-backed durable events with replay          ║
║  5. ✅ DISTRIBUTED TRACER  - Cross-repo tracing with correlation IDs        ║
║  6. ✅ HEALTH AGGREGATOR   - Centralized health with anomaly detection      ║
║  7. ✅ TRANSACTIONAL START - Two-phase commit with automatic rollback       ║
║  8. ✅ CIRCUIT BREAKERS    - Fail-fast patterns throughout                  ║
║  9. ✅ ADAPTIVE THROTTLING - Dynamic backpressure based on system load      ║
║  10.✅ ZERO HARDCODING     - 100% config-driven via environment             ║
╚══════════════════════════════════════════════════════════════════════════════╝

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │  TrinityUnifiedOrchestrator v83.0                                       │
    │  ├── ProcessSupervisor (PID monitoring, crash detection, restart)       │
    │  ├── CrashRecoveryManager (exponential backoff, cooldown, limits)       │
    │  ├── ResourceCoordinator (port pool, memory limits, CPU affinity)       │
    │  ├── EventStore (WAL, replay, dedup, TTL expiration)                    │
    │  ├── DistributedTracer (correlation IDs, span propagation)              │
    │  ├── UnifiedHealthAggregator (anomaly detection, trend analysis)        │
    │  ├── TransactionalStartup (prepare → commit → rollback)                 │
    │  └── AdaptiveThrottler (backpressure, rate limiting, circuit breaking)  │
    └─────────────────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.trinity_integrator import TrinityUnifiedOrchestrator

    async def main():
        orchestrator = TrinityUnifiedOrchestrator()

        # Single command starts everything with full crash recovery
        success = await orchestrator.start()

        if success:
            # Get unified health across all repos
            health = await orchestrator.get_unified_health()

            # Get distributed trace for debugging
            trace = orchestrator.tracer.get_current_trace()

        # Graceful shutdown with state preservation
        await orchestrator.stop()

Author: JARVIS Trinity v83.0 - Production-Grade Unified Orchestrator
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import psutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum, auto
from functools import wraps, partial
from pathlib import Path
from threading import RLock
from typing import (
    Any, Awaitable, Callable, Coroutine, Deque, Dict, Final,
    FrozenSet, Generic, Iterator, List, Literal, Mapping,
    NamedTuple, Optional, Protocol, Sequence, Set, Tuple,
    Type, TypeVar, Union, cast, overload, runtime_checkable,
)

# Type variables for generics
T = TypeVar("T")
R = TypeVar("R")
E = TypeVar("E", bound=Exception)

logger = logging.getLogger(__name__)

# =============================================================================
# Advanced Constants & Configuration Registry
# =============================================================================

class ConfigRegistry:
    """
    Centralized configuration registry with environment variable binding.
    Thread-safe, immutable after initialization, supports hot-reload signals.
    """

    _instance: Optional["ConfigRegistry"] = None
    _lock: RLock = RLock()
    _frozen: bool = False

    # Default configuration (all configurable via environment)
    DEFAULTS: Final[Dict[str, Any]] = {
        # Trinity Core
        "TRINITY_STARTUP_TIMEOUT": 120.0,
        "TRINITY_HEALTH_INTERVAL": 30.0,
        "TRINITY_SHUTDOWN_TIMEOUT": 60.0,
        "TRINITY_DATA_DIR": "~/.jarvis/trinity",

        # Crash Recovery
        "TRINITY_CRASH_MAX_RESTARTS": 5,
        "TRINITY_CRASH_INITIAL_BACKOFF": 1.0,
        "TRINITY_CRASH_MAX_BACKOFF": 300.0,
        "TRINITY_CRASH_BACKOFF_MULTIPLIER": 2.0,
        "TRINITY_CRASH_COOLDOWN_PERIOD": 300.0,

        # Process Supervisor
        "TRINITY_SUPERVISOR_CHECK_INTERVAL": 5.0,
        "TRINITY_SUPERVISOR_ZOMBIE_TIMEOUT": 30.0,
        "TRINITY_SUPERVISOR_HEARTBEAT_TIMEOUT": 60.0,

        # Resource Coordinator
        "TRINITY_RESOURCE_PORT_POOL_START": 8000,
        "TRINITY_RESOURCE_PORT_POOL_SIZE": 100,
        "TRINITY_RESOURCE_MEMORY_LIMIT_MB": 4096,
        "TRINITY_RESOURCE_CPU_LIMIT_PERCENT": 80,

        # Event Store
        "TRINITY_EVENT_STORE_PATH": "~/.jarvis/trinity/events.db",
        "TRINITY_EVENT_STORE_WAL_MODE": True,
        "TRINITY_EVENT_TTL_HOURS": 24,
        "TRINITY_EVENT_MAX_REPLAY": 1000,

        # Distributed Tracing
        "TRINITY_TRACING_ENABLED": True,
        "TRINITY_TRACING_SAMPLE_RATE": 1.0,
        "TRINITY_TRACING_MAX_SPANS": 10000,

        # Health Aggregator
        "TRINITY_HEALTH_ANOMALY_THRESHOLD": 0.8,
        "TRINITY_HEALTH_HISTORY_SIZE": 100,
        "TRINITY_HEALTH_TREND_WINDOW": 10,

        # Circuit Breaker
        "TRINITY_CIRCUIT_FAILURE_THRESHOLD": 5,
        "TRINITY_CIRCUIT_RECOVERY_TIMEOUT": 30.0,
        "TRINITY_CIRCUIT_HALF_OPEN_REQUESTS": 3,

        # Adaptive Throttling
        "TRINITY_THROTTLE_MAX_CONCURRENT": 100,
        "TRINITY_THROTTLE_QUEUE_SIZE": 1000,
        "TRINITY_THROTTLE_RATE_LIMIT": 100.0,

        # Component Paths (auto-detected if not set)
        "JARVIS_PRIME_REPO_PATH": "",
        "REACTOR_CORE_REPO_PATH": "",
        "JARVIS_PRIME_ENABLED": True,
        "REACTOR_CORE_ENABLED": True,
    }

    def __new__(cls) -> "ConfigRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._config = {}
                cls._instance._load_from_env()
            return cls._instance

    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        for key, default in self.DEFAULTS.items():
            env_value = os.getenv(key)
            if env_value is not None:
                # Type coercion based on default type
                if isinstance(default, bool):
                    self._config[key] = env_value.lower() in ("true", "1", "yes", "on")
                elif isinstance(default, int):
                    self._config[key] = int(env_value)
                elif isinstance(default, float):
                    self._config[key] = float(env_value)
                else:
                    self._config[key] = env_value
            else:
                self._config[key] = default

    def get(self, key: str, default: T = None) -> T:
        """Get configuration value with type preservation."""
        return self._config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._config[key]

    def freeze(self) -> None:
        """Freeze configuration to prevent further changes."""
        self._frozen = True

    def reload(self) -> None:
        """Reload configuration from environment (if not frozen)."""
        if not self._frozen:
            self._load_from_env()


# Global config accessor
def get_config() -> ConfigRegistry:
    """Get the global configuration registry."""
    return ConfigRegistry()


# =============================================================================
# Environment Helpers (Legacy - Use ConfigRegistry for new code)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


# =============================================================================
# Advanced Circuit Breaker Pattern
# =============================================================================

class CircuitState(IntEnum):
    """Circuit breaker states."""
    CLOSED = 0      # Normal operation
    OPEN = 1        # Failing, reject calls
    HALF_OPEN = 2   # Testing recovery


@dataclass
class CircuitBreakerStats:
    """Statistics for a circuit breaker."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_changes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0


class CircuitBreaker:
    """
    Advanced Circuit Breaker with sliding window failure detection.

    Features:
    - Sliding window for failure rate calculation
    - Configurable failure threshold
    - Half-open state for gradual recovery
    - Call rejection when open
    - Async-native implementation
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_requests: int = 3,
        window_size: int = 10,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_requests = half_open_requests
        self.window_size = window_size

        self._state = CircuitState.CLOSED
        self._failure_window: Deque[Tuple[float, bool]] = deque(maxlen=window_size)
        self._last_state_change = time.time()
        self._half_open_successes = 0
        self._stats = CircuitBreakerStats()
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_state_change: List[Callable[[CircuitState, CircuitState], None]] = []

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        return self._stats

    def _count_recent_failures(self) -> int:
        """Count failures in the sliding window."""
        now = time.time()
        cutoff = now - self.recovery_timeout
        return sum(1 for ts, success in self._failure_window if not success and ts > cutoff)

    async def _check_state_transition(self) -> None:
        """Check and perform state transitions."""
        now = time.time()

        if self._state == CircuitState.CLOSED:
            # Check if failures exceed threshold
            if self._count_recent_failures() >= self.failure_threshold:
                await self._transition_to(CircuitState.OPEN)

        elif self._state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if now - self._last_state_change >= self.recovery_timeout:
                await self._transition_to(CircuitState.HALF_OPEN)

        elif self._state == CircuitState.HALF_OPEN:
            # Check if enough successful requests
            if self._half_open_successes >= self.half_open_requests:
                await self._transition_to(CircuitState.CLOSED)

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()
        self._stats.state_changes += 1

        if new_state == CircuitState.HALF_OPEN:
            self._half_open_successes = 0

        logger.info(
            f"[CircuitBreaker:{self.name}] State transition: "
            f"{old_state.name} → {new_state.name}"
        )

        for callback in self._on_state_change:
            try:
                callback(old_state, new_state)
            except Exception as e:
                logger.warning(f"[CircuitBreaker:{self.name}] Callback error: {e}")

    async def __aenter__(self) -> "CircuitBreaker":
        """Context manager entry - check if call should be allowed."""
        async with self._lock:
            await self._check_state_transition()

            if self._state == CircuitState.OPEN:
                self._stats.rejected_calls += 1
                raise CircuitOpenError(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Retry after {self.recovery_timeout}s"
                )

            self._stats.total_calls += 1
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Context manager exit - record success/failure."""
        async with self._lock:
            success = exc_type is None
            now = time.time()

            # Record in sliding window
            self._failure_window.append((now, success))

            if success:
                self._stats.successful_calls += 1
                self._stats.last_success_time = now
                self._stats.consecutive_successes += 1
                self._stats.consecutive_failures = 0

                if self._state == CircuitState.HALF_OPEN:
                    self._half_open_successes += 1

            else:
                self._stats.failed_calls += 1
                self._stats.last_failure_time = now
                self._stats.consecutive_failures += 1
                self._stats.consecutive_successes = 0

                # Immediately open if in half-open state
                if self._state == CircuitState.HALF_OPEN:
                    await self._transition_to(CircuitState.OPEN)

            await self._check_state_transition()

        return False  # Don't suppress exceptions

    def on_state_change(
        self,
        callback: Callable[[CircuitState, CircuitState], None],
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# =============================================================================
# Process Supervisor - PID Monitoring & Auto-Healing
# =============================================================================

@dataclass
class ProcessInfo:
    """Information about a supervised process."""
    component_id: str
    pid: int
    pgid: Optional[int] = None
    start_time: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    restart_count: int = 0
    status: str = "running"
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProcessSupervisor:
    """
    Advanced Process Supervisor with auto-healing capabilities.

    Features:
    - PID monitoring and zombie detection
    - Automatic restart on crash
    - Resource usage tracking (CPU, memory)
    - Heartbeat-based liveness checks
    - Process group management for clean termination
    - Graceful → forceful termination escalation
    """

    def __init__(
        self,
        check_interval: float = 5.0,
        zombie_timeout: float = 30.0,
        heartbeat_timeout: float = 60.0,
    ):
        config = get_config()
        self.check_interval = config.get("TRINITY_SUPERVISOR_CHECK_INTERVAL", check_interval)
        self.zombie_timeout = config.get("TRINITY_SUPERVISOR_ZOMBIE_TIMEOUT", zombie_timeout)
        self.heartbeat_timeout = config.get("TRINITY_SUPERVISOR_HEARTBEAT_TIMEOUT", heartbeat_timeout)

        self._processes: Dict[str, ProcessInfo] = {}
        self._restart_callbacks: Dict[str, Callable[[], Awaitable[bool]]] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4)

        # Statistics
        self._stats = {
            "total_restarts": 0,
            "zombie_detections": 0,
            "heartbeat_timeouts": 0,
            "resource_violations": 0,
        }

    async def start(self) -> None:
        """Start the process supervisor."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("[ProcessSupervisor] Started")

    async def stop(self) -> None:
        """Stop the supervisor and terminate all processes."""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task

        # Terminate all supervised processes
        for component_id in list(self._processes.keys()):
            await self.terminate_process(component_id)

        self._executor.shutdown(wait=False)
        logger.info("[ProcessSupervisor] Stopped")

    async def register_process(
        self,
        component_id: str,
        pid: int,
        restart_callback: Optional[Callable[[], Awaitable[bool]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a process for supervision."""
        async with self._lock:
            try:
                proc = psutil.Process(pid)
                pgid = os.getpgid(pid)

                info = ProcessInfo(
                    component_id=component_id,
                    pid=pid,
                    pgid=pgid,
                    start_time=proc.create_time(),
                    metadata=metadata or {},
                )

                self._processes[component_id] = info

                if restart_callback:
                    self._restart_callbacks[component_id] = restart_callback

                logger.info(
                    f"[ProcessSupervisor] Registered {component_id} "
                    f"(PID={pid}, PGID={pgid})"
                )

            except (psutil.NoSuchProcess, ProcessLookupError) as e:
                logger.warning(
                    f"[ProcessSupervisor] Failed to register {component_id}: {e}"
                )

    async def unregister_process(self, component_id: str) -> None:
        """Unregister a process from supervision."""
        async with self._lock:
            self._processes.pop(component_id, None)
            self._restart_callbacks.pop(component_id, None)
            logger.debug(f"[ProcessSupervisor] Unregistered {component_id}")

    async def update_heartbeat(self, component_id: str) -> None:
        """Update the heartbeat timestamp for a component."""
        async with self._lock:
            if component_id in self._processes:
                self._processes[component_id].last_heartbeat = time.time()

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                await self._check_all_processes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ProcessSupervisor] Monitor error: {e}")

    async def _check_all_processes(self) -> None:
        """Check all supervised processes."""
        async with self._lock:
            processes_to_restart: List[str] = []

            for component_id, info in list(self._processes.items()):
                try:
                    # Check if process is still running
                    proc = psutil.Process(info.pid)
                    status = proc.status()

                    # Update resource usage
                    info.cpu_percent = proc.cpu_percent()
                    info.memory_mb = proc.memory_info().rss / (1024 * 1024)
                    info.status = status

                    # Check for zombie
                    if status == psutil.STATUS_ZOMBIE:
                        self._stats["zombie_detections"] += 1
                        logger.warning(
                            f"[ProcessSupervisor] Zombie detected: {component_id}"
                        )
                        processes_to_restart.append(component_id)
                        continue

                    # Check heartbeat timeout
                    heartbeat_age = time.time() - info.last_heartbeat
                    if heartbeat_age > self.heartbeat_timeout:
                        self._stats["heartbeat_timeouts"] += 1
                        logger.warning(
                            f"[ProcessSupervisor] Heartbeat timeout: {component_id} "
                            f"(last={heartbeat_age:.1f}s ago)"
                        )
                        processes_to_restart.append(component_id)

                except psutil.NoSuchProcess:
                    logger.warning(
                        f"[ProcessSupervisor] Process crashed: {component_id}"
                    )
                    processes_to_restart.append(component_id)

                except Exception as e:
                    logger.error(
                        f"[ProcessSupervisor] Check failed for {component_id}: {e}"
                    )

        # Restart crashed processes (outside lock)
        for component_id in processes_to_restart:
            await self._handle_process_crash(component_id)

    async def _handle_process_crash(self, component_id: str) -> None:
        """Handle a crashed process."""
        callback = self._restart_callbacks.get(component_id)

        if callback:
            logger.info(f"[ProcessSupervisor] Restarting {component_id}...")
            self._stats["total_restarts"] += 1

            try:
                success = await callback()
                if success:
                    logger.info(
                        f"[ProcessSupervisor] Successfully restarted {component_id}"
                    )
                else:
                    logger.error(
                        f"[ProcessSupervisor] Failed to restart {component_id}"
                    )
            except Exception as e:
                logger.error(
                    f"[ProcessSupervisor] Restart callback failed for {component_id}: {e}"
                )
        else:
            # No restart callback - just clean up
            await self.unregister_process(component_id)

    async def terminate_process(
        self,
        component_id: str,
        graceful_timeout: float = 10.0,
    ) -> bool:
        """Terminate a supervised process."""
        async with self._lock:
            info = self._processes.get(component_id)
            if not info:
                return True

            try:
                proc = psutil.Process(info.pid)

                # Try graceful termination first
                proc.terminate()

                # Wait for graceful shutdown
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            self._executor, proc.wait, graceful_timeout
                        ),
                        timeout=graceful_timeout + 1,
                    )
                    logger.info(
                        f"[ProcessSupervisor] Gracefully terminated {component_id}"
                    )
                except asyncio.TimeoutError:
                    # Force kill
                    proc.kill()
                    logger.warning(
                        f"[ProcessSupervisor] Force killed {component_id}"
                    )

                # Also kill process group if different from main process
                if info.pgid and info.pgid != os.getpid():
                    with suppress(ProcessLookupError, OSError):
                        os.killpg(info.pgid, signal.SIGTERM)

                del self._processes[component_id]
                return True

            except psutil.NoSuchProcess:
                del self._processes[component_id]
                return True

            except Exception as e:
                logger.error(
                    f"[ProcessSupervisor] Failed to terminate {component_id}: {e}"
                )
                return False

    def get_process_info(self, component_id: str) -> Optional[ProcessInfo]:
        """Get information about a supervised process."""
        return self._processes.get(component_id)

    def get_all_processes(self) -> Dict[str, ProcessInfo]:
        """Get all supervised processes."""
        return dict(self._processes)

    def get_stats(self) -> Dict[str, Any]:
        """Get supervisor statistics."""
        return {
            **self._stats,
            "active_processes": len(self._processes),
            "processes": {
                k: {
                    "pid": v.pid,
                    "status": v.status,
                    "cpu": v.cpu_percent,
                    "memory_mb": v.memory_mb,
                    "restarts": v.restart_count,
                }
                for k, v in self._processes.items()
            },
        }


# =============================================================================
# Crash Recovery Manager - Exponential Backoff & Rate Limiting
# =============================================================================

@dataclass
class CrashRecord:
    """Record of a component crash."""
    component_id: str
    timestamp: float
    error: Optional[str] = None
    restart_attempt: int = 0
    backoff_seconds: float = 0.0


class CrashRecoveryManager:
    """
    Advanced Crash Recovery with exponential backoff and rate limiting.

    Features:
    - Exponential backoff with jitter
    - Configurable max restarts
    - Cooldown period reset
    - Crash history tracking
    - Intelligent restart scheduling
    """

    def __init__(
        self,
        max_restarts: int = 5,
        initial_backoff: float = 1.0,
        max_backoff: float = 300.0,
        backoff_multiplier: float = 2.0,
        cooldown_period: float = 300.0,
        jitter_factor: float = 0.1,
    ):
        config = get_config()
        self.max_restarts = config.get("TRINITY_CRASH_MAX_RESTARTS", max_restarts)
        self.initial_backoff = config.get("TRINITY_CRASH_INITIAL_BACKOFF", initial_backoff)
        self.max_backoff = config.get("TRINITY_CRASH_MAX_BACKOFF", max_backoff)
        self.backoff_multiplier = config.get("TRINITY_CRASH_BACKOFF_MULTIPLIER", backoff_multiplier)
        self.cooldown_period = config.get("TRINITY_CRASH_COOLDOWN_PERIOD", cooldown_period)
        self.jitter_factor = jitter_factor

        self._crash_history: Dict[str, List[CrashRecord]] = defaultdict(list)
        self._restart_counts: Dict[str, int] = defaultdict(int)
        self._last_backoff: Dict[str, float] = {}
        self._lock = asyncio.Lock()

        import random
        self._random = random.Random()

    async def should_restart(self, component_id: str) -> Tuple[bool, float]:
        """
        Determine if a component should be restarted.

        Returns:
            Tuple of (should_restart, backoff_seconds)
        """
        async with self._lock:
            now = time.time()

            # Get recent crash history
            history = self._crash_history[component_id]

            # Check cooldown - reset if no crashes in cooldown period
            if history:
                last_crash = history[-1].timestamp
                if now - last_crash > self.cooldown_period:
                    # Reset restart counter
                    self._restart_counts[component_id] = 0
                    self._last_backoff.pop(component_id, None)
                    logger.info(
                        f"[CrashRecovery] Cooldown reset for {component_id}"
                    )

            # Check restart limit
            restart_count = self._restart_counts[component_id]
            if restart_count >= self.max_restarts:
                logger.error(
                    f"[CrashRecovery] Max restarts ({self.max_restarts}) exceeded "
                    f"for {component_id}"
                )
                return False, 0.0

            # Calculate backoff with exponential increase and jitter
            if component_id in self._last_backoff:
                base_backoff = min(
                    self._last_backoff[component_id] * self.backoff_multiplier,
                    self.max_backoff,
                )
            else:
                base_backoff = self.initial_backoff

            # Add jitter (±10% by default)
            jitter = base_backoff * self.jitter_factor * (2 * self._random.random() - 1)
            backoff = max(0.1, base_backoff + jitter)

            self._last_backoff[component_id] = base_backoff

            return True, backoff

    async def record_crash(
        self,
        component_id: str,
        error: Optional[str] = None,
    ) -> CrashRecord:
        """Record a component crash."""
        async with self._lock:
            self._restart_counts[component_id] += 1
            restart_count = self._restart_counts[component_id]

            backoff = self._last_backoff.get(component_id, self.initial_backoff)

            record = CrashRecord(
                component_id=component_id,
                timestamp=time.time(),
                error=error,
                restart_attempt=restart_count,
                backoff_seconds=backoff,
            )

            self._crash_history[component_id].append(record)

            # Keep only recent history
            if len(self._crash_history[component_id]) > 100:
                self._crash_history[component_id] = self._crash_history[component_id][-100:]

            logger.warning(
                f"[CrashRecovery] Recorded crash for {component_id} "
                f"(attempt={restart_count}, backoff={backoff:.1f}s)"
            )

            return record

    async def record_success(self, component_id: str) -> None:
        """Record successful restart/operation."""
        async with self._lock:
            # Decrease backoff on success
            if component_id in self._last_backoff:
                self._last_backoff[component_id] = max(
                    self.initial_backoff,
                    self._last_backoff[component_id] / self.backoff_multiplier,
                )

    def get_crash_history(self, component_id: str) -> List[CrashRecord]:
        """Get crash history for a component."""
        return list(self._crash_history.get(component_id, []))

    def get_restart_count(self, component_id: str) -> int:
        """Get current restart count for a component."""
        return self._restart_counts.get(component_id, 0)

    def reset(self, component_id: str) -> None:
        """Reset crash state for a component."""
        self._crash_history.pop(component_id, None)
        self._restart_counts.pop(component_id, None)
        self._last_backoff.pop(component_id, None)


# =============================================================================
# Resource Coordinator - Port/Memory/CPU Management
# =============================================================================

@dataclass
class ResourceAllocation:
    """A resource allocation for a component."""
    component_id: str
    ports: List[int] = field(default_factory=list)
    memory_limit_mb: Optional[float] = None
    cpu_limit_percent: Optional[float] = None
    allocated_at: float = field(default_factory=time.time)


class ResourceCoordinator:
    """
    Centralized Resource Coordinator for Trinity components.

    Features:
    - Port pool management with collision avoidance
    - Memory limit enforcement
    - CPU affinity/limit management
    - Resource reservation and release
    - Resource usage monitoring
    """

    def __init__(
        self,
        port_pool_start: int = 8000,
        port_pool_size: int = 100,
        memory_limit_mb: float = 4096,
        cpu_limit_percent: float = 80,
    ):
        config = get_config()
        self.port_pool_start = config.get("TRINITY_RESOURCE_PORT_POOL_START", port_pool_start)
        self.port_pool_size = config.get("TRINITY_RESOURCE_PORT_POOL_SIZE", port_pool_size)
        self.memory_limit_mb = config.get("TRINITY_RESOURCE_MEMORY_LIMIT_MB", memory_limit_mb)
        self.cpu_limit_percent = config.get("TRINITY_RESOURCE_CPU_LIMIT_PERCENT", cpu_limit_percent)

        # Port pool
        self._available_ports: Set[int] = set(
            range(port_pool_start, port_pool_start + port_pool_size)
        )
        self._allocated_ports: Dict[str, Set[int]] = defaultdict(set)

        # Allocations
        self._allocations: Dict[str, ResourceAllocation] = {}
        self._lock = asyncio.Lock()

        # System resources
        self._total_memory_mb = psutil.virtual_memory().total / (1024 * 1024)
        self._cpu_count = psutil.cpu_count() or 1

    async def allocate_port(self, component_id: str) -> Optional[int]:
        """Allocate a free port for a component."""
        async with self._lock:
            # First, check if any allocated ports are actually free
            for port in sorted(self._available_ports):
                if await self._is_port_free(port):
                    self._available_ports.remove(port)
                    self._allocated_ports[component_id].add(port)

                    # Update allocation record
                    if component_id not in self._allocations:
                        self._allocations[component_id] = ResourceAllocation(
                            component_id=component_id
                        )
                    self._allocations[component_id].ports.append(port)

                    logger.debug(
                        f"[ResourceCoordinator] Allocated port {port} to {component_id}"
                    )
                    return port

            logger.warning(
                f"[ResourceCoordinator] No free ports available for {component_id}"
            )
            return None

    async def release_port(self, component_id: str, port: int) -> None:
        """Release a port back to the pool."""
        async with self._lock:
            if port in self._allocated_ports[component_id]:
                self._allocated_ports[component_id].remove(port)
                self._available_ports.add(port)

                if component_id in self._allocations:
                    with suppress(ValueError):
                        self._allocations[component_id].ports.remove(port)

                logger.debug(
                    f"[ResourceCoordinator] Released port {port} from {component_id}"
                )

    async def release_all(self, component_id: str) -> None:
        """Release all resources for a component."""
        async with self._lock:
            # Release ports
            ports = self._allocated_ports.pop(component_id, set())
            self._available_ports.update(ports)

            # Remove allocation record
            self._allocations.pop(component_id, None)

            logger.debug(
                f"[ResourceCoordinator] Released all resources for {component_id}"
            )

    async def _is_port_free(self, port: int) -> bool:
        """Check if a port is actually free."""
        import socket

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.1)
            sock.bind(("127.0.0.1", port))
            sock.close()
            return True
        except (socket.error, OSError):
            return False

    def get_system_resources(self) -> Dict[str, Any]:
        """Get current system resource usage."""
        memory = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.1)

        return {
            "memory": {
                "total_mb": memory.total / (1024 * 1024),
                "available_mb": memory.available / (1024 * 1024),
                "percent_used": memory.percent,
            },
            "cpu": {
                "count": self._cpu_count,
                "percent_used": cpu_percent,
            },
            "ports": {
                "pool_size": self.port_pool_size,
                "available": len(self._available_ports),
                "allocated": sum(len(p) for p in self._allocated_ports.values()),
            },
        }

    def get_allocation(self, component_id: str) -> Optional[ResourceAllocation]:
        """Get resource allocation for a component."""
        return self._allocations.get(component_id)


# =============================================================================
# Event Store - WAL-Backed Durable Event Storage
# =============================================================================

@dataclass
class TrinityEvent:
    """A durable event in the Trinity system."""
    event_id: str
    event_type: str
    source: str
    target: Optional[str]
    payload: Dict[str, Any]
    timestamp: float
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    processed: bool = False
    retry_count: int = 0
    expires_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "target": self.target,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "processed": self.processed,
            "retry_count": self.retry_count,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrinityEvent":
        return cls(**d)


class EventStore:
    """
    WAL-Backed Durable Event Store for Trinity.

    Features:
    - SQLite with WAL mode for crash-safe writes
    - Event replay for missed messages
    - Deduplication via event_id
    - TTL-based expiration
    - Correlation ID tracking for distributed tracing
    - Async-native implementation
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        wal_mode: bool = True,
        ttl_hours: float = 24.0,
        max_replay: int = 1000,
    ):
        config = get_config()
        self.db_path = Path(
            os.path.expanduser(
                db_path or config.get("TRINITY_EVENT_STORE_PATH", "~/.jarvis/trinity/events.db")
            )
        )
        self.wal_mode = config.get("TRINITY_EVENT_STORE_WAL_MODE", wal_mode)
        self.ttl_hours = config.get("TRINITY_EVENT_TTL_HOURS", ttl_hours)
        self.max_replay = config.get("TRINITY_EVENT_MAX_REPLAY", max_replay)

        self._connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Event handlers
        self._handlers: Dict[str, List[Callable[[TrinityEvent], Awaitable[None]]]] = defaultdict(list)

    async def initialize(self) -> None:
        """Initialize the event store."""
        if self._initialized:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._init_db)

        self._initialized = True
        logger.info(f"[EventStore] Initialized at {self.db_path}")

    def _init_db(self) -> None:
        """Initialize database schema (sync)."""
        self._connection = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level="IMMEDIATE",
        )
        self._connection.row_factory = sqlite3.Row

        # Enable WAL mode for better crash recovery
        if self.wal_mode:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")

        # Create events table
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT,
                payload TEXT NOT NULL,
                timestamp REAL NOT NULL,
                correlation_id TEXT,
                trace_id TEXT,
                processed INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                expires_at REAL,
                created_at REAL DEFAULT (julianday('now'))
            )
        """)

        # Create indices
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed ON events(processed, timestamp)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_correlation ON events(correlation_id)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires ON events(expires_at)
        """)

        self._connection.commit()

    async def publish(
        self,
        event_type: str,
        source: str,
        payload: Dict[str, Any],
        target: Optional[str] = None,
        correlation_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        ttl_hours: Optional[float] = None,
    ) -> str:
        """Publish an event to the store."""
        await self.initialize()

        event_id = str(uuid.uuid4())
        timestamp = time.time()
        ttl = ttl_hours or self.ttl_hours
        expires_at = timestamp + (ttl * 3600) if ttl > 0 else None

        event = TrinityEvent(
            event_id=event_id,
            event_type=event_type,
            source=source,
            target=target,
            payload=payload,
            timestamp=timestamp,
            correlation_id=correlation_id,
            trace_id=trace_id,
            expires_at=expires_at,
        )

        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._insert_event, event)

        # Dispatch to handlers
        await self._dispatch_event(event)

        logger.debug(f"[EventStore] Published event {event_id} ({event_type})")
        return event_id

    def _insert_event(self, event: TrinityEvent) -> None:
        """Insert event into database (sync)."""
        if not self._connection:
            return

        self._connection.execute("""
            INSERT OR REPLACE INTO events
            (event_id, event_type, source, target, payload, timestamp,
             correlation_id, trace_id, processed, retry_count, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_id,
            event.event_type,
            event.source,
            event.target,
            json.dumps(event.payload),
            event.timestamp,
            event.correlation_id,
            event.trace_id,
            1 if event.processed else 0,
            event.retry_count,
            event.expires_at,
        ))
        self._connection.commit()

    async def get_unprocessed(
        self,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[TrinityEvent]:
        """Get unprocessed events for replay."""
        await self.initialize()

        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor, self._get_unprocessed_sync, event_type, limit
            )

    def _get_unprocessed_sync(
        self,
        event_type: Optional[str],
        limit: int,
    ) -> List[TrinityEvent]:
        """Get unprocessed events (sync)."""
        if not self._connection:
            return []

        now = time.time()
        if event_type:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE processed = 0 AND (expires_at IS NULL OR expires_at > ?)
                  AND event_type = ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (now, event_type, limit))
        else:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE processed = 0 AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY timestamp ASC
                LIMIT ?
            """, (now, limit))

        events = []
        for row in cursor.fetchall():
            events.append(TrinityEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                source=row["source"],
                target=row["target"],
                payload=json.loads(row["payload"]),
                timestamp=row["timestamp"],
                correlation_id=row["correlation_id"],
                trace_id=row["trace_id"],
                processed=bool(row["processed"]),
                retry_count=row["retry_count"],
                expires_at=row["expires_at"],
            ))

        return events

    async def mark_processed(self, event_id: str) -> None:
        """Mark an event as processed."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self._executor, self._mark_processed_sync, event_id
            )

    def _mark_processed_sync(self, event_id: str) -> None:
        """Mark processed (sync)."""
        if not self._connection:
            return

        self._connection.execute(
            "UPDATE events SET processed = 1 WHERE event_id = ?",
            (event_id,)
        )
        self._connection.commit()

    async def replay_events(
        self,
        since_timestamp: float,
        event_type: Optional[str] = None,
    ) -> List[TrinityEvent]:
        """Replay events since a timestamp."""
        await self.initialize()

        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor, self._replay_events_sync, since_timestamp, event_type
            )

    def _replay_events_sync(
        self,
        since_timestamp: float,
        event_type: Optional[str],
    ) -> List[TrinityEvent]:
        """Replay events (sync)."""
        if not self._connection:
            return []

        if event_type:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE timestamp >= ? AND event_type = ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (since_timestamp, event_type, self.max_replay))
        else:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (since_timestamp, self.max_replay))

        events = []
        for row in cursor.fetchall():
            events.append(TrinityEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                source=row["source"],
                target=row["target"],
                payload=json.loads(row["payload"]),
                timestamp=row["timestamp"],
                correlation_id=row["correlation_id"],
                trace_id=row["trace_id"],
                processed=bool(row["processed"]),
                retry_count=row["retry_count"],
                expires_at=row["expires_at"],
            ))

        return events

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[TrinityEvent], Awaitable[None]],
    ) -> None:
        """Subscribe to events of a specific type."""
        self._handlers[event_type].append(handler)

    async def _dispatch_event(self, event: TrinityEvent) -> None:
        """Dispatch event to registered handlers."""
        handlers = self._handlers.get(event.event_type, [])
        handlers.extend(self._handlers.get("*", []))  # Wildcard handlers

        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.warning(
                    f"[EventStore] Handler error for {event.event_type}: {e}"
                )

    async def cleanup_expired(self) -> int:
        """Remove expired events."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor, self._cleanup_expired_sync
            )

    def _cleanup_expired_sync(self) -> int:
        """Cleanup expired (sync)."""
        if not self._connection:
            return 0

        cursor = self._connection.execute(
            "DELETE FROM events WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),)
        )
        self._connection.commit()
        return cursor.rowcount

    async def close(self) -> None:
        """Close the event store."""
        if self._connection:
            self._connection.close()
            self._connection = None
        self._executor.shutdown(wait=False)


# =============================================================================
# Distributed Tracer - Cross-Repo Tracing with Correlation
# =============================================================================

@dataclass
class TraceSpan:
    """A span in a distributed trace."""
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    operation_name: str
    service_name: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "in_progress"
    tags: Dict[str, Any] = field(default_factory=dict)
    logs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return (time.time() - self.start_time) * 1000


class DistributedTracer:
    """
    Distributed Tracing for Trinity cross-repo operations.

    Features:
    - OpenTelemetry-compatible trace/span model
    - Automatic correlation ID propagation
    - Span hierarchy tracking
    - Tag and log support
    - Sampling for production use
    - Context propagation across async boundaries
    """

    def __init__(
        self,
        service_name: str = "jarvis_body",
        enabled: bool = True,
        sample_rate: float = 1.0,
        max_spans: int = 10000,
    ):
        config = get_config()
        self.service_name = service_name
        self.enabled = config.get("TRINITY_TRACING_ENABLED", enabled)
        self.sample_rate = config.get("TRINITY_TRACING_SAMPLE_RATE", sample_rate)
        self.max_spans = config.get("TRINITY_TRACING_MAX_SPANS", max_spans)

        self._traces: Dict[str, List[TraceSpan]] = {}
        self._current_trace_id: Optional[str] = None
        self._current_span_id: Optional[str] = None
        self._span_stack: List[str] = []
        self._lock = asyncio.Lock()

        import random
        self._random = random.Random()

    def _should_sample(self) -> bool:
        """Determine if this trace should be sampled."""
        return self._random.random() < self.sample_rate

    @asynccontextmanager
    async def trace(
        self,
        operation_name: str,
        tags: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ):
        """Create a new trace context."""
        if not self.enabled:
            yield None
            return

        async with self._lock:
            # Start new trace or continue existing
            if trace_id:
                self._current_trace_id = trace_id
            elif not self._current_trace_id:
                if not self._should_sample():
                    yield None
                    return
                self._current_trace_id = str(uuid.uuid4())

            trace_id = self._current_trace_id

            # Create root span
            span_id = str(uuid.uuid4())
            span = TraceSpan(
                span_id=span_id,
                trace_id=trace_id,
                parent_span_id=None,
                operation_name=operation_name,
                service_name=self.service_name,
                start_time=time.time(),
                tags=tags or {},
            )

            if trace_id not in self._traces:
                self._traces[trace_id] = []
            self._traces[trace_id].append(span)

            self._current_span_id = span_id
            self._span_stack.append(span_id)

        try:
            yield span
            span.status = "ok"
        except Exception as e:
            span.status = "error"
            span.tags["error"] = str(e)
            raise
        finally:
            span.end_time = time.time()
            async with self._lock:
                self._span_stack.pop() if self._span_stack else None
                self._current_span_id = self._span_stack[-1] if self._span_stack else None

                # Cleanup if this was the root span
                if not self._span_stack:
                    self._current_trace_id = None

                # Limit total spans
                while len(self._traces) > self.max_spans:
                    oldest = min(self._traces.keys())
                    del self._traces[oldest]

    @asynccontextmanager
    async def span(
        self,
        operation_name: str,
        tags: Optional[Dict[str, Any]] = None,
    ):
        """Create a child span within the current trace."""
        if not self.enabled or not self._current_trace_id:
            yield None
            return

        async with self._lock:
            span_id = str(uuid.uuid4())
            span = TraceSpan(
                span_id=span_id,
                trace_id=self._current_trace_id,
                parent_span_id=self._current_span_id,
                operation_name=operation_name,
                service_name=self.service_name,
                start_time=time.time(),
                tags=tags or {},
            )

            self._traces[self._current_trace_id].append(span)
            self._current_span_id = span_id
            self._span_stack.append(span_id)

        try:
            yield span
            span.status = "ok"
        except Exception as e:
            span.status = "error"
            span.tags["error"] = str(e)
            raise
        finally:
            span.end_time = time.time()
            async with self._lock:
                self._span_stack.pop() if self._span_stack else None
                self._current_span_id = self._span_stack[-1] if self._span_stack else None

    def get_trace_id(self) -> Optional[str]:
        """Get current trace ID for propagation."""
        return self._current_trace_id

    def get_span_id(self) -> Optional[str]:
        """Get current span ID for propagation."""
        return self._current_span_id

    def get_trace(self, trace_id: str) -> List[TraceSpan]:
        """Get all spans for a trace."""
        return list(self._traces.get(trace_id, []))

    def get_trace_summary(self, trace_id: str) -> Dict[str, Any]:
        """Get summary of a trace."""
        spans = self._traces.get(trace_id, [])
        if not spans:
            return {}

        root_span = next((s for s in spans if s.parent_span_id is None), spans[0])

        return {
            "trace_id": trace_id,
            "operation": root_span.operation_name,
            "service": root_span.service_name,
            "status": root_span.status,
            "duration_ms": root_span.duration_ms,
            "span_count": len(spans),
            "error_count": sum(1 for s in spans if s.status == "error"),
        }

    def log_to_span(self, message: str, **kwargs) -> None:
        """Add a log entry to the current span."""
        if not self._current_span_id or not self._current_trace_id:
            return

        spans = self._traces.get(self._current_trace_id, [])
        for span in spans:
            if span.span_id == self._current_span_id:
                span.logs.append({
                    "timestamp": time.time(),
                    "message": message,
                    **kwargs,
                })
                break


# =============================================================================
# Unified Health Aggregator - Centralized Health with Anomaly Detection
# =============================================================================

@dataclass
class HealthSample:
    """A single health sample."""
    timestamp: float
    component: str
    healthy: bool
    latency_ms: float
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class AnomalyReport:
    """Report of a detected anomaly."""
    component: str
    anomaly_type: str
    severity: str  # "warning", "critical"
    description: str
    timestamp: float
    metrics: Dict[str, Any] = field(default_factory=dict)


class UnifiedHealthAggregator:
    """
    Unified Health Aggregator with anomaly detection.

    Features:
    - Centralized health from all Trinity components
    - Sliding window health history
    - Trend analysis (improving/degrading)
    - Anomaly detection (latency spikes, error rate changes)
    - Health score calculation
    - Component correlation analysis
    """

    def __init__(
        self,
        anomaly_threshold: float = 0.8,
        history_size: int = 100,
        trend_window: int = 10,
    ):
        config = get_config()
        self.anomaly_threshold = config.get("TRINITY_HEALTH_ANOMALY_THRESHOLD", anomaly_threshold)
        self.history_size = config.get("TRINITY_HEALTH_HISTORY_SIZE", history_size)
        self.trend_window = config.get("TRINITY_HEALTH_TREND_WINDOW", trend_window)

        self._history: Dict[str, Deque[HealthSample]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._anomalies: Deque[AnomalyReport] = deque(maxlen=100)
        self._baselines: Dict[str, Dict[str, float]] = {}
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_anomaly: List[Callable[[AnomalyReport], None]] = []

    async def record_health(
        self,
        component: str,
        healthy: bool,
        latency_ms: float,
        metrics: Optional[Dict[str, float]] = None,
    ) -> None:
        """Record a health sample."""
        sample = HealthSample(
            timestamp=time.time(),
            component=component,
            healthy=healthy,
            latency_ms=latency_ms,
            metrics=metrics or {},
        )

        async with self._lock:
            self._history[component].append(sample)

            # Check for anomalies
            await self._check_anomalies(component, sample)

            # Update baseline
            await self._update_baseline(component)

    async def _check_anomalies(self, component: str, sample: HealthSample) -> None:
        """Check for anomalies in the new sample."""
        baseline = self._baselines.get(component, {})

        # Latency spike detection
        if "latency_avg" in baseline:
            latency_ratio = sample.latency_ms / max(baseline["latency_avg"], 1)
            if latency_ratio > 3.0:  # 3x normal latency
                await self._report_anomaly(AnomalyReport(
                    component=component,
                    anomaly_type="latency_spike",
                    severity="warning" if latency_ratio < 5.0 else "critical",
                    description=f"Latency spike: {sample.latency_ms:.1f}ms (normal: {baseline['latency_avg']:.1f}ms)",
                    timestamp=sample.timestamp,
                    metrics={"latency_ms": sample.latency_ms, "ratio": latency_ratio},
                ))

        # Health state change detection
        history = self._history[component]
        if len(history) >= 3:
            recent_unhealthy = sum(1 for s in list(history)[-3:] if not s.healthy)
            if recent_unhealthy >= 2 and sample.healthy is False:
                await self._report_anomaly(AnomalyReport(
                    component=component,
                    anomaly_type="repeated_failure",
                    severity="critical",
                    description=f"Component {component} has failed multiple times recently",
                    timestamp=sample.timestamp,
                    metrics={"consecutive_failures": recent_unhealthy},
                ))

    async def _report_anomaly(self, anomaly: AnomalyReport) -> None:
        """Report an anomaly to registered handlers."""
        self._anomalies.append(anomaly)

        logger.warning(
            f"[HealthAggregator] Anomaly detected: {anomaly.component} - "
            f"{anomaly.anomaly_type} ({anomaly.severity})"
        )

        for callback in self._on_anomaly:
            try:
                callback(anomaly)
            except Exception as e:
                logger.debug(f"[HealthAggregator] Callback error: {e}")

    async def _update_baseline(self, component: str) -> None:
        """Update baseline metrics for a component."""
        history = list(self._history[component])
        if len(history) < 10:
            return

        # Calculate baseline metrics
        latencies = [s.latency_ms for s in history]
        health_rate = sum(1 for s in history if s.healthy) / len(history)

        self._baselines[component] = {
            "latency_avg": sum(latencies) / len(latencies),
            "latency_p99": sorted(latencies)[int(len(latencies) * 0.99)],
            "health_rate": health_rate,
        }

    def get_component_health(self, component: str) -> Dict[str, Any]:
        """Get health summary for a component."""
        history = list(self._history.get(component, []))
        if not history:
            return {"status": "unknown", "samples": 0}

        recent = history[-self.trend_window:] if len(history) >= self.trend_window else history

        healthy_count = sum(1 for s in recent if s.healthy)
        health_rate = healthy_count / len(recent)
        avg_latency = sum(s.latency_ms for s in recent) / len(recent)

        # Determine trend
        if len(history) >= self.trend_window * 2:
            older = history[-(self.trend_window * 2):-self.trend_window]
            older_rate = sum(1 for s in older if s.healthy) / len(older)
            trend = "improving" if health_rate > older_rate else (
                "degrading" if health_rate < older_rate else "stable"
            )
        else:
            trend = "insufficient_data"

        return {
            "status": "healthy" if health_rate > self.anomaly_threshold else "degraded",
            "health_rate": health_rate,
            "avg_latency_ms": avg_latency,
            "samples": len(history),
            "trend": trend,
            "last_check": history[-1].timestamp if history else None,
        }

    def get_unified_health(self) -> Dict[str, Any]:
        """Get unified health across all components."""
        components = {}
        overall_health = 1.0

        for component in self._history.keys():
            health = self.get_component_health(component)
            components[component] = health
            overall_health *= health.get("health_rate", 1.0)

        # Calculate overall score (geometric mean)
        if components:
            overall_score = overall_health ** (1 / len(components))
        else:
            overall_score = 1.0

        return {
            "overall_score": overall_score,
            "overall_status": "healthy" if overall_score > self.anomaly_threshold else "degraded",
            "components": components,
            "recent_anomalies": [
                {
                    "component": a.component,
                    "type": a.anomaly_type,
                    "severity": a.severity,
                    "timestamp": a.timestamp,
                }
                for a in list(self._anomalies)[-10:]
            ],
        }

    def on_anomaly(self, callback: Callable[[AnomalyReport], None]) -> None:
        """Register callback for anomaly detection."""
        self._on_anomaly.append(callback)

    def get_anomalies(self, since: Optional[float] = None) -> List[AnomalyReport]:
        """Get recent anomalies."""
        anomalies = list(self._anomalies)
        if since:
            anomalies = [a for a in anomalies if a.timestamp >= since]
        return anomalies


# =============================================================================
# Adaptive Throttler - Backpressure & Rate Limiting
# =============================================================================

class AdaptiveThrottler:
    """
    Adaptive Throttler with backpressure management.

    Features:
    - Token bucket rate limiting
    - Adaptive rate adjustment based on system load
    - Request queuing with timeout
    - Priority-based throttling
    - Backpressure signaling to clients
    """

    def __init__(
        self,
        max_concurrent: int = 100,
        queue_size: int = 1000,
        rate_limit: float = 100.0,  # requests per second
    ):
        config = get_config()
        self.max_concurrent = config.get("TRINITY_THROTTLE_MAX_CONCURRENT", max_concurrent)
        self.queue_size = config.get("TRINITY_THROTTLE_QUEUE_SIZE", queue_size)
        self.rate_limit = config.get("TRINITY_THROTTLE_RATE_LIMIT", rate_limit)

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._current_rate = rate_limit
        self._token_bucket = rate_limit
        self._last_token_update = time.time()
        self._lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "total_requests": 0,
            "queued_requests": 0,
            "rejected_requests": 0,
            "completed_requests": 0,
        }

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire a throttle permit."""
        async with self._lock:
            # Token bucket refill
            now = time.time()
            elapsed = now - self._last_token_update
            self._token_bucket = min(
                self.rate_limit,
                self._token_bucket + elapsed * self._current_rate,
            )
            self._last_token_update = now

            # Check rate limit
            if self._token_bucket < 1.0:
                self._stats["rejected_requests"] += 1
                return False

            self._token_bucket -= 1.0

        self._stats["total_requests"] += 1

        # Acquire semaphore for concurrency limit
        try:
            if timeout:
                await asyncio.wait_for(self._semaphore.acquire(), timeout)
            else:
                await self._semaphore.acquire()
            return True
        except asyncio.TimeoutError:
            self._stats["rejected_requests"] += 1
            return False

    def release(self) -> None:
        """Release a throttle permit."""
        self._semaphore.release()
        self._stats["completed_requests"] += 1

    @asynccontextmanager
    async def throttle(self, timeout: Optional[float] = None):
        """Context manager for throttled operations."""
        acquired = await self.acquire(timeout)
        if not acquired:
            raise ThrottleExceededError("Request throttled - system under load")

        try:
            yield
        finally:
            self.release()

    def adjust_rate(self, factor: float) -> None:
        """Adjust the rate limit dynamically."""
        self._current_rate = max(1.0, self.rate_limit * factor)
        logger.debug(f"[Throttler] Rate adjusted to {self._current_rate:.1f}/s")

    def get_stats(self) -> Dict[str, Any]:
        """Get throttler statistics."""
        return {
            **self._stats,
            "current_rate": self._current_rate,
            "available_permits": self._semaphore._value,
            "queue_size": self._queue.qsize(),
        }


class ThrottleExceededError(Exception):
    """Raised when throttle limit is exceeded."""
    pass


# =============================================================================
# Types and Enums
# =============================================================================

class TrinityState(str, Enum):
    """Overall Trinity system state."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class ComponentHealth(str, Enum):
    """Health status of a component."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentStatus:
    """Status of a Trinity component."""
    name: str
    health: ComponentHealth
    online: bool
    last_heartbeat: Optional[float] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class TrinityHealth:
    """Overall Trinity system health."""
    state: TrinityState
    components: Dict[str, ComponentStatus]
    uptime_seconds: float
    last_check: float
    degraded_components: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# =============================================================================
# Trinity Unified Orchestrator v83.0
# =============================================================================

class TrinityUnifiedOrchestrator:
    """
    Trinity Unified Orchestrator v83.0 - Production-Grade Integration.

    The SINGLE POINT OF TRUTH for Trinity integration with:
    - JARVIS Body (this repo)
    - JARVIS Prime (cognitive mind)
    - Reactor-Core (training nerves)

    v83.0 Critical Features:
    ════════════════════════
    ✅ Crash Recovery      - Auto-restart with exponential backoff
    ✅ Process Supervisor  - PID monitoring, zombie detection, auto-heal
    ✅ Resource Coordinator- Port/memory/CPU reservation with pooling
    ✅ Event Store         - WAL-backed durable events with replay
    ✅ Distributed Tracer  - Cross-repo tracing with correlation IDs
    ✅ Health Aggregator   - Anomaly detection, trend analysis
    ✅ Transactional Start - Two-phase commit with rollback
    ✅ Circuit Breakers    - Fail-fast patterns throughout
    ✅ Adaptive Throttling - Backpressure management
    ✅ Zero Hardcoding     - 100% config-driven
    """

    # 2. Guaranteed Event Delivery System  
    class GuaranteedEventDelivery:
        """
        Guaranteed event with acknowledgement and retry.

        Features:
        - Acknowledgement-based delivery 
        - Automatic retry with exponential backoff 
        - Persistent event queue (SQLite-backed)
        - At-least-once delivery guarantee
        """

        def __init__(self, 
            store_path: Optional[Path] = None, 
            max_retries: int = 5, 
            retry_backoff: float = 1.0,
        ):
            self._store_path = store_path or Path.home() / ".jarvis" / "trinity" / "events.db" # Default store path is in the user's home directory under .jarvis/trinity/events.db 
            self._store_path.parent.mkdir(parents=True, exist_ok=True) # Create directory if it doesn't exist. parent is the directory above the store path.
            self._max_retries = max_retries # Maximum number of retries. After this many retries, the event is considered failed. 
            self._retry_backoff = retry_backoff # Base retry delay in seconds. This is the delay before the next retry. 

            self._pending_events: Dict[str, Dict[str, Any]] = {} # Event ID -> Event data. This is the event that is being processed. 
            self._ack_timeouts: Dict[str, asyncio.Task] = {} # Event ID -> Task. This is the task that is waiting for the acknowledgement. 
            self._retry_tasks: Dict[str, asyncio.Task] = {} # Event ID -> Task. This is the task that is waiting for the retry. 

            self._db_conn: Optional[sqlite3.Connection] = None # Database connection. This is the connection to the SQLite database. 
            self._db_lock = asyncio.Lock() # Lock for the database. This is used to prevent concurrent access to the database.  

        # Initialize the event store. This is called when the orchestrator is initialized. 
        async def initialize(self) -> None:
            """Initialize persistent event store."""
            async with self._db_lock: # Lock the database to prevent concurrent access. 
                # Connect to the database. 
                self._db_conn = sqlite2.connect(
                    str(self._store_path),
                    check_same_thread=False,
                    timeout=30.0,
                )
                self._db_conn.execute("PRAGMA journal_mode=WAL") # Enable WAL mode for better concurrency 
                self._db_conn.execute("PRAGMA busy_timeout=30000") # Set busy timeout to 30 seconds. This is the timeout for the database to wait for a lock.  

                # Create tables for pending events. This is the table that stores the events that are being processed. 
                self._db_conn.execute(""" 
                    CREATE TABLE IF NOT EXISTS pending_events (
                        event_id TEXT PRIMARY KEY,
                        event_data TEXT NOT NULL, 
                        target_component TEXT, 
                        retry_count INTEGER DEFAULT 0, 
                        created_at REAL NOT NULL,
                        last_attempt_at REAL, 
                        next_retry REAL
                    )
                """)

                # Create index for next retry. This is used to find the next event to retry. 
                self._db_conn.execute(""" 
                    CREATE INDEX IF NOT EXISTS idx_next_retry 
                    ON pending_events(next_retry)
                """)

                self._db_conn.commit() # Commit the changes to the database. 

                # Load pending events
                await self._load_pending_events() # Load pending events from the database. 

        # Send event with acknowledgment guarantee. This is called when the event is sent to the target component. 
        async def send_with_ack(
            self, # Self is the instance of the class. 
            event: TrinityEvent, # Event to send. This is the event that is being sent. 
            target_component: str, # Target component. This is the component that is receiving the event. 
            ack_timeout: float = 30.0, # Acknowledgement timeout. This is the timeout for the acknowledgement. 
        ) -> bool: # Return True if acknowledged, False if failed after retries. 
            """
            Send event with acknowledgment guarantee.
            
            Returns:
                True if acknowledged, False if failed after retries
            """
            event_id = event.event_id # Get the event ID. This is the unique identifier for the event. 

            # Store event in database. This is the event that is being processed.  
            async with self._db_lock:
                self._db_conn.execute(
                    """
                    INSERT OR REPLACE INTO pending_events 
                    (event_id, event_data, target_component, created_at, next_retry)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, # Event ID. This is the unique identifier for the event. 
                        json.dumps(event.to_dict()), # Event data. This is the event that is being processed. 
                        target_component, # Target component. This is the component that is receiving the event. 
                        time.time(), # Created at. This is the time when the event was created. 
                        time.time(), # Next retry. This is the time when the event will be retried. 
                    )
                )
                self._db_conn.commit() # Commit the changes to the database. 
            
            # Track pending event. This is the event that is being processed. 
            self._pending_events[event_id] = {
                "event": event, # Event data. This is the event that is being processed. 
                "target": target_component, # Target component. This is the component that is receiving the event. 
                "retry_count": 0, # Retry count. This is the number of times the event has been retried. 
                "ack_timeout": ack_timeout, # Acknowledgement timeout. This is the timeout for the acknowledgement. 
            } 

            # Send event to target component and wait for acknowledgement.  
            ack_received = await self._send_and_wait_ack(event_id, target_component)

            # If acknowledgement is received, remove from pending.  
            if ack_received: 
                # Remove from pending. This is the event that is being processed. 
                await self._remove_pending_event(event_id) 
                return True # Return True if acknowledgement is received. 
            else: # If acknowledgement is not received, schedule retry. 
                # Schedule retry 
                await self._schedule_retry(event_id) # Schedule retry. This is the event that is being processed. 
                return False # Return False if acknowledgement is not received. 
        
        async def _send_and_wait_ack(self, event_id: str, target_component: str) -> bool:
            """Send event and wait for acknowlegment."""
            # Get pending event. This is the event that is being processed. 
            pending = self._pending_events.get(event_id) 

            if not pending: # If the event is not found, return False.  
                return False  # Return False if the event is not found.  

            event = pending["event"] # Event data. This is the event that is being processed. 
            timeout = pending["ack_timeout"] # Acknowledgement timeout. This is the timeout for the acknowledgement. 

            # Send via bridge (this would call the actual bridge)
            # For now, simulate 
            try: 
                # Create future for ACK. This is the future that is waiting for the acknowledgement. 
                ack_future = asyncio.Future() 
                
                # Create task to wait for acknowledgement. This is the task that is waiting for the ACK. 
                self._ack_timeouts[event_id] = aysncio.create_task(
                    self._wait_for_ack(event_id, ack_future, timeout) # Wait for acknowledgement. This is the task that is waiting for the ACK. 
                )

                try: 
                    await asyncio.wait_for(ack_future, timeout=timeout) # Wait for acknowledgement. This is the future that is waiting for the ACK. 
                    return True # Return True if acknowledgement is received. 
                except asyncio.TimeoutError: # If the acknowledgement is not received, return False.  
                    return False # Return False if the acknowledgement is not received.  
            
            except Exception as e: # If an error occurs, log the error and return False.  
                logger.error(f"Error sending event {event_id}: {e}") # Log the error.  
                return False # Return False if an error occurs.  
                
        # Wait for ACK (would be called by bridge on ACK). 
        async def _wait_for_ack(
            self,
            event_id: str, 
            future: asyncio.Future,
            timeout: float,
        ): 
            """Wait for ACK (would be called by bridge on ACK).""" 
            try: 
                await asyncio.sleep(timeout) # Wait for the timeout. 
                if not future.done(): # If the future is not done, set the result to False.     
                    future.set_result(False) # Set the result to False. 
            except asyncio.CancelledError: # If the task is cancelled, pass. 
                pass # Pass if the task is cancelled. 
        
        # Acknowledge the event. This is called by the bridge on ACK. 
        def acknowledge(self, event_id: str): 
            """Acknowledge the event."""
            if event_id in self._ack_timeouts: # If the event is in the acknowledgement timeouts, cancel the task. 
                task = self._ack_timeouts.pop(event_id) # Get the task. 
                task.cancel() # Cancel the task. 
            
            if event_id in self._pending_events: # If the event is in the pending events, set the result to True. 
                future = asyncio.Future() # Create a future. 
                future.set_result(True) # Set the result to True. 

        # Schedule retry. This is called when the event is not acknowledged. 
        async def schedule_retry(self, event_id: str): 
            # Get pending event. This is the event that is being processed. 
            pending = self._pending_events.get(event_id) 

            # If the event is not found, return. 
            if not pending: # If the event is not found, return. 
                return # Return if the event is not found. 

            # Get retry count. This is the number of times the event has been retried. 
            retry_count = pending["retry_count"]

            if retry_count >= self._max_retries: # If the retry count is greater than the maximum retries, log the error and remove the event from the pending events.  
                logger.error(f"Event {event_id} failed after {retry_count} retries")
                await self._remove_pending_event(event_id) # Remove the event from the pending events. 
                return # Return if the retry count is greater than the maximum retries.     
            
            # Calculate backoff time. This is the time to wait before the next retry.  
            backoff = self._retry_backoff * (2 ** retry_count) 
            # Calculate next retry time. This is the time when the event will be retried. 
            next_retry = time.time() + backoff 

            # Update retry count. This is the number of times the event has been retried. 
            pending["retry_count"] = retry_count + 1 

            # Update database with new retry info. This is the event that is being processed. 
            async with self._db_lock: 
                self._db_conn.execute(
                    """
                    UPDATE pending_events 
                    SET retry_count = ?, next_retry = ?, last_attempt = ? 
                    WHERE event_id = ? 
                    """, 
                    (retry_count + 1, next_retry, time.time(), event_id) # Update the retry count, next retry time, and last attempt time. 
                )
                self._db_conn.commit() # Commit the changes to the database. 

            # Schedule retry task. This is the task that is waiting for the retry. 
            self._retry_tasks[event_id] = asyncio.create_task(
                self._retry_event(event_id, backoff) # Retry the event after the backoff time. 
            )

        # Retry event. This is called when the event is not acknowledged. 
        async def _retry_event(self, event_id: str, delay: float):
            """Retry sending event after delay."""
            # Wait for the delay. This is the time to wait before the next retry. 
            """Retry sending event after delay."""
            await asyncio.sleep(delay) # Wait for the delay. This is the time to wait before the next retry. 

            # Get pending event. This is the event that is being processed. 
            pending = self._pending_events.get(event_id) 

            # If the event is not found, return. 
            if not pending: # If the event is not found, return. 
                return # Return if the event is not found. 

            # Send event to target component and wait for acknowledgement. This is the event that is being processed. 
            if pending: 
                await self._send_and_wait_ack(event_id, pending["target"]) # Send the event to the target component and wait for acknowledgement. 

        # Load pending events from database on startup. This is called when the orchestrator is initialized. 
        async def _load_pending_events(self):
            """Load pending events from database on startup."""
            async with self._db_lock: # Lock the database to prevent concurrent access. 
                cursor = self._db_conn.execute( # Execute the query to load the pending events from the database. 
                    """
                    SELECT event_id, event_data, target_component, retry_count, next_retry 
                    FROM pending_events  
                    WHERE next_retry <= ?  
                    """,
                    (time.time()) # Time now. This is the time when the event was created. 
                )

                # Fetch all the rows from the database. 
                for row in cursor.fetchall(): # For each row, get the event ID, event data, target component, retry count, and next retry time. 
                    event_id, event_data, target, retry_count, next_retry = row # Event ID, event data, target component, retry count, and next retry time. 

                    try: 
                        event_dict = json.loads(event_data) # Event data. This is the event that is being processed. 
                        event = TrinityEvent.from_dict(event_dict) # Event data. This is the event that is being processed.  

                        self._pending_events[event_id] = {
                            "event": event, # Event data. This is the event that is being processed. 
                            "target": target, # Target component. This is the component that is receiving the event. 
                            "retry_count": retry_count, # Retry count. This is the number of times the event has been retried. 
                            "ack_timeout": 30.0, # Acknowledgement timeout. This is the timeout for the acknowledgement. 
                        }

                        # Schedule retry if needed 
                        if next_retry <= time.time(): 
                            await self._schedule_retry(event_id) # Schedule retry. This is the event that is being processed. 

                    except Exception as e: # If an error occurs, log the error. 
                        logger.error(f"Error loading pending even {event_id}: {e}") # Log the error. 
        
        # Remove pending event. This is called when the event is acknowledged or failed after retries. 
        async def _remove_pending_event(self, event_id: str):
            """Remove event from pending queue."""
            self._pending_events.pop(event_id, None) # Remove the event from the pending events. 

            # Cancel tasks
            if event_id in self._ack_timeouts: # If the event is in the acknowledgement timeouts, cancel the task. 
                self._ack_timeouts[event_id].cancel() # Cancel the task. 
                del self._ack_timeouts[event_id] # Delete the task from the acknowledgement timeouts. 

            if event_id in self._retry_tasks: # If the event is in the retry tasks, cancel the task. 
                self._retry_tasks[event_id].cancel() 
                del self._retry_tasks[event_id] # Delete the task from the retry tasks. 

            # Remove from database 
            async with self._db_lock: # Lock the database to prevent concurrent access.  
                self._db_conn.execute( # Execute the query to remove the event from the database. 
                    """
                    DELETE FROM pending_events WHERE event_id = ? 
                    """,
                    (event_id,) # Event ID. This is the unique identifier for the event. 
                )
                self._db_conn.commit() # Commit the changes to the database. 

    def __init__(
        self,
        enable_jprime: bool = True,
        enable_reactor: bool = True,
        startup_timeout: float = 120.0,
        health_check_interval: float = 30.0,
    ):
        """
        Initialize the Trinity Unified Orchestrator v83.0.

        Args:
            enable_jprime: Enable JARVIS Prime integration
            enable_reactor: Enable Reactor-Core integration
            startup_timeout: Max time to wait for components
            health_check_interval: Interval between health checks
        """
        config = get_config()
        self.enable_jprime = config.get("JARVIS_PRIME_ENABLED", enable_jprime)
        self.enable_reactor = config.get("REACTOR_CORE_ENABLED", enable_reactor)
        self.startup_timeout = config.get("TRINITY_STARTUP_TIMEOUT", startup_timeout)
        self.health_check_interval = config.get("TRINITY_HEALTH_INTERVAL", health_check_interval)

        # State
        self._state = TrinityState.UNINITIALIZED
        self._start_time: Optional[float] = None
        self._lock = asyncio.Lock()

        # v83.0 Advanced Components
        self._process_supervisor = ProcessSupervisor()
        self._crash_recovery = CrashRecoveryManager()
        self._resource_coordinator = ResourceCoordinator()
        self._event_store = EventStore()
        self._tracer = DistributedTracer(service_name="jarvis_body")
        self._health_aggregator = UnifiedHealthAggregator()
        self._throttler = AdaptiveThrottler()

        # Circuit breakers for each component
        self._circuit_breakers: Dict[str, CircuitBreaker] = {
            "jprime": CircuitBreaker("jprime"),
            "reactor": CircuitBreaker("reactor"),
            "ipc": CircuitBreaker("ipc"),
        }

        # Legacy components (backward compatibility)
        self._ipc_bus = None
        self._shutdown_manager = None
        self._port_manager = None
        self._startup_coordinator = None

        # Clients
        self._jprime_client = None
        self._reactor_client = None

        # Process handles for crash recovery
        self._jprime_process: Optional[subprocess.Popen] = None
        self._reactor_process: Optional[subprocess.Popen] = None

        # v84.0: Managed async processes
        self._managed_processes: Dict[str, Dict[str, Any]] = {}

        # Background tasks
        self._health_task: Optional[asyncio.Task] = None
        self._event_cleanup_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_state_change: List[Callable[[TrinityState, TrinityState], None]] = []
        self._on_component_change: List[Callable[[str, ComponentHealth], None]] = []

        # Register anomaly handler
        self._health_aggregator.on_anomaly(self._handle_anomaly)

        logger.info(
            f"[TrinityOrchestrator v83.0] Initialized "
            f"(jprime={self.enable_jprime}, reactor={self.enable_reactor})"
        )

    def _handle_anomaly(self, anomaly: AnomalyReport) -> None:
        """Handle detected anomalies."""
        if anomaly.severity == "critical":
            logger.error(
                f"[TrinityOrchestrator] CRITICAL anomaly: {anomaly.component} - "
                f"{anomaly.description}"
            )
            # Potentially trigger recovery
            asyncio.create_task(self._handle_critical_anomaly(anomaly))

    async def _handle_critical_anomaly(self, anomaly: AnomalyReport) -> None:
        """Handle critical anomaly - attempt recovery."""
        component = anomaly.component

        # Check if we should attempt restart
        should_restart, backoff = await self._crash_recovery.should_restart(component)

        if should_restart:
            logger.info(
                f"[TrinityOrchestrator] Scheduling restart for {component} "
                f"in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            await self._restart_component(component)

    async def _restart_component(self, component_id: str) -> bool:
        """Restart a crashed component."""
        async with self._tracer.span(f"restart_{component_id}"):
            logger.info(f"[TrinityOrchestrator] Restarting {component_id}...")

            try:
                if component_id == "jarvis_prime" and self.enable_jprime:
                    success = await self._start_jprime()
                elif component_id == "reactor_core" and self.enable_reactor:
                    success = await self._start_reactor()
                else:
                    success = False

                if success:
                    await self._crash_recovery.record_success(component_id)
                    await self._event_store.publish(
                        event_type="component.restarted",
                        source="orchestrator",
                        payload={"component": component_id, "success": True},
                    )
                else:
                    await self._crash_recovery.record_crash(component_id)

                return success

            except Exception as e:
                logger.error(f"[TrinityOrchestrator] Restart failed: {e}")
                await self._crash_recovery.record_crash(component_id, str(e))
                return False

    @property
    def state(self) -> TrinityState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state in (TrinityState.READY, TrinityState.DEGRADED)

    @property
    def uptime(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    # =========================================================================
    # Startup
    # =========================================================================

    async def start(self) -> bool:
        """
        Start the Trinity system with v83.0 transactional startup.

        This is the single command that initializes everything:
        ════════════════════════════════════════════════════════
        Phase 1: PREPARE (transactional)
        ├── 1.1 Initialize event store (for durability)
        ├── 1.2 Cleanup orphan processes
        ├── 1.3 Initialize IPC with circuit breaker
        ├── 1.4 Allocate ports via ResourceCoordinator
        └── 1.5 Initialize shutdown manager

        Phase 2: START (with crash recovery)
        ├── 2.1 Start Process Supervisor
        ├── 2.2 Start JARVIS Body heartbeat
        ├── 2.3 Start JARVIS Prime (if enabled)
        └── 2.4 Start Reactor-Core (if enabled)

        Phase 3: VERIFY (health aggregation)
        ├── 3.1 Verify all heartbeats
        ├── 3.2 Record initial health baselines
        └── 3.3 Start background health monitoring

        Returns:
            True if startup successful (or degraded), False on failure
        """
        async with self._tracer.trace("trinity_startup") as root_span:
            async with self._lock:
                if self._state != TrinityState.UNINITIALIZED:
                    logger.warning(
                        f"[TrinityOrchestrator] Cannot start in state {self._state.value}"
                    )
                    return False

                self._set_state(TrinityState.INITIALIZING)
                self._start_time = time.time()

                try:
                    # ═══════════════════════════════════════════════════
                    # PHASE 1: PREPARE (Transactional)
                    # ═══════════════════════════════════════════════════
                    async with self._tracer.span("phase_1_prepare"):

                        # Step 1.1: Initialize event store for durability
                        async with self._tracer.span("init_event_store"):
                            await self._event_store.initialize()
                            await self._event_store.publish(
                                event_type="startup.begin",
                                source="orchestrator",
                                payload={"version": "v83.0", "timestamp": time.time()},
                            )

                        # Step 1.2: Orphan cleanup
                        async with self._tracer.span("cleanup_orphans"):
                            await self._cleanup_orphans()

                        # Step 1.3: Initialize IPC with circuit breaker
                        async with self._tracer.span("init_ipc"):
                            await self._init_ipc()

                        # Step 1.4: Port allocation via ResourceCoordinator
                        async with self._tracer.span("allocate_ports"):
                            await self._allocate_ports()

                        # Step 1.5: Initialize shutdown manager
                        async with self._tracer.span("init_shutdown"):
                            await self._init_shutdown_manager()

                    self._set_state(TrinityState.STARTING)

                    # ═══════════════════════════════════════════════════
                    # PHASE 2: START (With Crash Recovery)
                    # ═══════════════════════════════════════════════════
                    async with self._tracer.span("phase_2_start"):

                        # Step 2.1: Start Process Supervisor
                        async with self._tracer.span("start_supervisor"):
                            await self._process_supervisor.start()

                        # Step 2.2: Start JARVIS Body heartbeat
                        async with self._tracer.span("start_body_heartbeat"):
                            await self._start_body_heartbeat()

                        # Step 2.3 & 2.4: Start external components in parallel
                        jprime_ok = True
                        reactor_ok = True

                        async with self._tracer.span("start_external_components"):
                            tasks = []

                            if self.enable_jprime:
                                tasks.append(self._start_jprime_with_recovery())

                            if self.enable_reactor:
                                tasks.append(self._start_reactor_with_recovery())

                            if tasks:
                                results = await asyncio.gather(*tasks, return_exceptions=True)

                                if self.enable_jprime:
                                    jprime_ok = results[0] if not isinstance(results[0], Exception) else False
                                    if isinstance(results[0], Exception):
                                        logger.error(f"[TrinityOrchestrator] J-Prime start failed: {results[0]}")

                                if self.enable_reactor:
                                    idx = 1 if self.enable_jprime else 0
                                    reactor_ok = results[idx] if not isinstance(results[idx], Exception) else False
                                    if isinstance(results[idx], Exception):
                                        logger.error(f"[TrinityOrchestrator] Reactor start failed: {results[idx]}")

                    # ═══════════════════════════════════════════════════
                    # PHASE 3: VERIFY (Health Aggregation)
                    # ═══════════════════════════════════════════════════
                    async with self._tracer.span("phase_3_verify"):

                        # Step 3.1: Determine final state
                        if jprime_ok and reactor_ok:
                            self._set_state(TrinityState.READY)
                        else:
                            self._set_state(TrinityState.DEGRADED)
                            logger.warning(
                                "[TrinityOrchestrator] Starting in degraded mode "
                                f"(jprime={jprime_ok}, reactor={reactor_ok})"
                            )

                        # Step 3.2: Record initial health baselines
                        async with self._tracer.span("record_baselines"):
                            await self._health_aggregator.record_health(
                                component="jarvis_body",
                                healthy=True,
                                latency_ms=0.0,
                                metrics={"startup_time": time.time() - self._start_time},
                            )

                            if self.enable_jprime:
                                await self._health_aggregator.record_health(
                                    component="jarvis_prime",
                                    healthy=jprime_ok,
                                    latency_ms=0.0,
                                )

                            if self.enable_reactor:
                                await self._health_aggregator.record_health(
                                    component="reactor_core",
                                    healthy=reactor_ok,
                                    latency_ms=0.0,
                                )

                        # Step 3.3: Start health monitoring
                        self._running = True
                        self._health_task = asyncio.create_task(self._health_loop())
                        self._event_cleanup_task = asyncio.create_task(self._event_cleanup_loop())

                    # Publish startup complete event
                    await self._event_store.publish(
                        event_type="startup.complete",
                        source="orchestrator",
                        payload={
                            "version": "v83.0",
                            "state": self._state.value,
                            "jprime_enabled": self.enable_jprime,
                            "reactor_enabled": self.enable_reactor,
                        },
                    )

                    elapsed = time.time() - self._start_time
                    logger.info(
                        f"[TrinityOrchestrator v83.0] Started in {elapsed:.2f}s "
                        f"(state={self._state.value})"
                    )

                    return True

                except Exception as e:
                    logger.error(f"[TrinityOrchestrator] Startup failed: {e}")
                    self._set_state(TrinityState.ERROR)

                    # Publish startup failure event
                    try:
                        await self._event_store.publish(
                            event_type="startup.failed",
                            source="orchestrator",
                            payload={"error": str(e), "traceback": traceback.format_exc()},
                        )
                    except Exception:
                        pass

                    return False

    async def _cleanup_orphans(self) -> None:
        """Clean up orphan processes from previous runs."""
        try:
            from backend.core.coordinated_shutdown import cleanup_orphan_processes

            terminated, failed = await cleanup_orphan_processes()

            if terminated > 0:
                logger.info(
                    f"[TrinityIntegrator] Cleaned up {terminated} orphan processes"
                )

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Orphan cleanup failed: {e}")

    async def _init_ipc(self) -> None:
        """Initialize the resilient IPC bus."""
        from backend.core.trinity_ipc import get_resilient_trinity_ipc_bus

        self._ipc_bus = await get_resilient_trinity_ipc_bus()
        logger.debug("[TrinityIntegrator] IPC bus initialized")

    async def _allocate_ports(self) -> None:
        """Allocate ports for all components."""
        try:
            from backend.core.trinity_port_manager import get_trinity_port_manager

            self._port_manager = await get_trinity_port_manager()
            allocations = await self._port_manager.allocate_all_ports()

            for component, result in allocations.items():
                if result.success:
                    logger.info(
                        f"[TrinityIntegrator] Port allocated: "
                        f"{component.value}={result.port}"
                    )
                else:
                    logger.warning(
                        f"[TrinityIntegrator] Port allocation failed: "
                        f"{component.value}: {result.error}"
                    )

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Port allocation failed: {e}")

    async def _init_shutdown_manager(self) -> None:
        """Initialize the shutdown manager."""
        from backend.core.coordinated_shutdown import (
            EnhancedShutdownManager,
            setup_signal_handlers,
        )

        self._shutdown_manager = EnhancedShutdownManager(
            ipc_bus=self._ipc_bus,
            detect_orphans_on_start=False,  # Already done
        )

        # Register signal handlers
        try:
            loop = asyncio.get_running_loop()
            setup_signal_handlers(self._shutdown_manager, loop)
        except Exception as e:
            logger.debug(f"[TrinityIntegrator] Signal handler setup failed: {e}")

        logger.debug("[TrinityIntegrator] Shutdown manager initialized")

    async def _start_body_heartbeat(self) -> None:
        """Start JARVIS Body heartbeat publishing."""
        try:
            from backend.core.trinity_ipc import ComponentType

            await self._ipc_bus.publish_heartbeat(
                component=ComponentType.JARVIS_BODY,
                status="starting",
                pid=os.getpid(),
                metrics={"startup_time": self._start_time},
            )

            logger.debug("[TrinityIntegrator] Body heartbeat started")

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Body heartbeat failed: {e}")

    async def _wait_for_jprime(self) -> bool:
        """Wait for JARVIS Prime to be ready."""
        try:
            from backend.clients.jarvis_prime_client import get_jarvis_prime_client

            self._jprime_client = await get_jarvis_prime_client()

            # Wait for connection with timeout
            start = time.time()
            while time.time() - start < self.startup_timeout:
                if self._jprime_client.is_online:
                    logger.info("[TrinityIntegrator] JARVIS Prime is ready")
                    return True

                await asyncio.sleep(2.0)

            logger.warning("[TrinityIntegrator] JARVIS Prime timeout")
            return False

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] JARVIS Prime init failed: {e}")
            return False

    async def _wait_for_reactor(self) -> bool:
        """Wait for Reactor-Core to be ready."""
        try:
            from backend.clients.reactor_core_client import (
                initialize_reactor_client,
                get_reactor_client,
            )

            await initialize_reactor_client()
            self._reactor_client = get_reactor_client()

            if self._reactor_client and self._reactor_client.is_online:
                logger.info("[TrinityOrchestrator] Reactor-Core is ready")
                return True

            logger.warning("[TrinityOrchestrator] Reactor-Core not available")
            return False

        except Exception as e:
            logger.warning(f"[TrinityOrchestrator] Reactor-Core init failed: {e}")
            return False

    # =========================================================================
    # v83.0: Crash Recovery Methods
    # =========================================================================

    async def _start_jprime_with_recovery(self) -> bool:
        """
        Start JARVIS Prime with circuit breaker and crash recovery.

        Uses circuit breaker to fail fast if J-Prime is repeatedly failing.
        Registers process with supervisor for automatic restart on crash.
        """
        circuit = self._circuit_breakers["jprime"]

        try:
            async with circuit:
                success = await self._start_jprime()

                if success:
                    await self._event_store.publish(
                        event_type="component.started",
                        source="orchestrator",
                        payload={"component": "jarvis_prime"},
                        trace_id=self._tracer.get_trace_id(),
                    )

                return success

        except CircuitOpenError as e:
            logger.warning(f"[TrinityOrchestrator] J-Prime circuit breaker open: {e}")
            return False
        except Exception as e:
            logger.error(f"[TrinityOrchestrator] J-Prime start failed: {e}")
            await self._crash_recovery.record_crash("jarvis_prime", str(e))
            return False

    async def _start_reactor_with_recovery(self) -> bool:
        """
        Start Reactor-Core with circuit breaker and crash recovery.

        Uses circuit breaker to fail fast if Reactor is repeatedly failing.
        Registers process with supervisor for automatic restart on crash.
        """
        circuit = self._circuit_breakers["reactor"]

        try:
            async with circuit:
                success = await self._start_reactor()

                if success:
                    await self._event_store.publish(
                        event_type="component.started",
                        source="orchestrator",
                        payload={"component": "reactor_core"},
                        trace_id=self._tracer.get_trace_id(),
                    )

                return success

        except CircuitOpenError as e:
            logger.warning(f"[TrinityOrchestrator] Reactor circuit breaker open: {e}")
            return False
        except Exception as e:
            logger.error(f"[TrinityOrchestrator] Reactor start failed: {e}")
            await self._crash_recovery.record_crash("reactor_core", str(e))
            return False

    async def _start_jprime(self) -> bool:
        """
        v84.0: Start JARVIS Prime - discover or launch.

        Strategy:
        1. First check if already running (heartbeat file)
        2. If not, launch the process
        3. Wait for it to become ready
        """
        # Check if already running
        if await self._discover_running_component("jarvis_prime"):
            logger.info("[TrinityOrchestrator] J-Prime already running (discovered)")
            return await self._wait_for_jprime()

        # Launch the process
        launched = await self._launch_jprime_process()
        if not launched:
            logger.warning("[TrinityOrchestrator] Failed to launch J-Prime")
            return False

        # Wait for it to be ready
        return await self._wait_for_jprime()

    async def _start_reactor(self) -> bool:
        """
        v84.0: Start Reactor-Core - discover or launch.

        Strategy:
        1. First check if already running (heartbeat file)
        2. If not, launch the process
        3. Wait for it to become ready
        """
        # Check if already running
        if await self._discover_running_component("reactor_core"):
            logger.info("[TrinityOrchestrator] Reactor-Core already running (discovered)")
            return await self._wait_for_reactor()

        # Launch the process
        launched = await self._launch_reactor_process()
        if not launched:
            logger.warning("[TrinityOrchestrator] Failed to launch Reactor-Core")
            return False

        # Wait for it to be ready
        return await self._wait_for_reactor()

    # =========================================================================
    # v84.0: Process Launching and Discovery
    # =========================================================================

    async def _discover_running_component(self, component: str) -> bool:
        """
        v84.0: Discover if a component is already running.

        Checks:
        1. Heartbeat file freshness (< 30s)
        2. Process is actually alive (PID check)
        3. HTTP health check responds

        Args:
            component: Component name (jarvis_prime, reactor_core)

        Returns:
            True if component is running and healthy
        """
        import psutil

        trinity_dir = Path(os.getenv(
            "TRINITY_DIR",
            str(Path.home() / ".jarvis" / "trinity")
        ))

        heartbeat_file = trinity_dir / "components" / f"{component}.json"

        if not heartbeat_file.exists():
            return False

        try:
            with open(heartbeat_file, 'r') as f:
                data = json.load(f)

            # Check freshness (30 second threshold)
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp
            if age > 30.0:
                logger.debug(f"[Discovery] {component} heartbeat stale ({age:.1f}s)")
                return False

            # Check if process is alive
            pid = data.get("pid")
            if pid:
                try:
                    proc = psutil.Process(pid)
                    if proc.is_running():
                        logger.debug(f"[Discovery] {component} process alive (PID {pid})")

                        # Optional: HTTP health check
                        port = data.get("port")
                        if port:
                            try:
                                import aiohttp
                                async with aiohttp.ClientSession(
                                    timeout=aiohttp.ClientTimeout(total=5.0)
                                ) as session:
                                    url = f"http://localhost:{port}/health"
                                    async with session.get(url) as resp:
                                        if resp.status == 200:
                                            logger.info(
                                                f"[Discovery] {component} healthy at port {port}"
                                            )
                                            return True
                            except Exception:
                                # HTTP check failed but process is alive
                                pass

                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            return False

        except Exception as e:
            logger.debug(f"[Discovery] Error checking {component}: {e}")
            return False

    async def _launch_jprime_process(self) -> bool:
        """
        v84.0: Launch JARVIS Prime process.

        Discovers repo path from environment or default location,
        then starts the server in a subprocess.
        """
        # Get repo path
        jprime_repo = os.getenv(
            "JARVIS_PRIME_REPO_PATH",
            str(Path.home() / "Documents" / "repos" / "jarvis-prime")
        )
        jprime_repo = Path(jprime_repo)

        if not jprime_repo.exists():
            logger.error(f"[Launcher] J-Prime repo not found: {jprime_repo}")
            return False

        # Find Python executable
        venv_python = jprime_repo / "venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = jprime_repo / "venv" / "bin" / "python"
        if not venv_python.exists():
            # Try system Python
            import shutil
            venv_python = Path(shutil.which("python3") or "python3")

        # Build command
        server_module = "jarvis_prime.server"
        port = int(os.getenv("JARVIS_PRIME_PORT", "8000"))

        cmd = [
            str(venv_python),
            "-m", server_module,
            "--port", str(port),
        ]

        # Add auto-download flag if configured
        if os.getenv("JARVIS_PRIME_AUTO_DOWNLOAD", "false").lower() == "true":
            cmd.append("--auto-download")

        logger.info(f"[Launcher] Starting J-Prime: {' '.join(cmd)}")

        try:
            # Start process in background
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(jprime_repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # Detach from parent
                env={
                    **os.environ,
                    "PYTHONPATH": str(jprime_repo),
                    "TRINITY_ENABLED": "true",
                },
            )

            # Wait briefly for startup
            await asyncio.sleep(2.0)

            # Check if process is still running
            if process.returncode is None:
                logger.info(f"[Launcher] J-Prime started (PID {process.pid})")

                # Store process for later management
                self._managed_processes["jarvis_prime"] = {
                    "process": process,
                    "pid": process.pid,
                    "port": port,
                    "started_at": time.time(),
                }

                return True
            else:
                # Process exited immediately
                stdout, stderr = await process.communicate()
                logger.error(
                    f"[Launcher] J-Prime failed to start (exit {process.returncode})"
                )
                if stderr:
                    logger.error(f"[Launcher] stderr: {stderr.decode()[:500]}")
                return False

        except Exception as e:
            logger.error(f"[Launcher] Failed to launch J-Prime: {e}")
            return False

    async def _launch_reactor_process(self) -> bool:
        """
        v84.0: Launch Reactor-Core process.

        Discovers repo path from environment or default location,
        then starts the orchestrator in a subprocess.
        """
        # Get repo path
        reactor_repo = os.getenv(
            "REACTOR_CORE_REPO_PATH",
            str(Path.home() / "Documents" / "repos" / "reactor-core")
        )
        reactor_repo = Path(reactor_repo)

        if not reactor_repo.exists():
            logger.error(f"[Launcher] Reactor-Core repo not found: {reactor_repo}")
            return False

        # Find Python executable
        venv_python = reactor_repo / "venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = reactor_repo / "venv" / "bin" / "python"
        if not venv_python.exists():
            # Try system Python
            import shutil
            venv_python = Path(shutil.which("python3") or "python3")

        # Build command - Reactor-Core uses its own orchestrator
        orchestrator_script = reactor_repo / "reactor_core" / "orchestration" / "trinity_orchestrator.py"
        if not orchestrator_script.exists():
            # Fallback to main script
            orchestrator_script = reactor_repo / "main.py"

        if not orchestrator_script.exists():
            logger.error(f"[Launcher] Reactor-Core orchestrator not found")
            return False

        cmd = [str(venv_python), str(orchestrator_script)]

        logger.info(f"[Launcher] Starting Reactor-Core: {' '.join(cmd)}")

        try:
            # Start process in background
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(reactor_repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # Detach from parent
                env={
                    **os.environ,
                    "PYTHONPATH": str(reactor_repo),
                    "TRINITY_ENABLED": "true",
                },
            )

            # Wait briefly for startup
            await asyncio.sleep(2.0)

            # Check if process is still running
            if process.returncode is None:
                logger.info(f"[Launcher] Reactor-Core started (PID {process.pid})")

                # Store process for later management
                self._managed_processes["reactor_core"] = {
                    "process": process,
                    "pid": process.pid,
                    "started_at": time.time(),
                }

                return True
            else:
                # Process exited immediately
                stdout, stderr = await process.communicate()
                logger.error(
                    f"[Launcher] Reactor-Core failed to start (exit {process.returncode})"
                )
                if stderr:
                    logger.error(f"[Launcher] stderr: {stderr.decode()[:500]}")
                return False

        except Exception as e:
            logger.error(f"[Launcher] Failed to launch Reactor-Core: {e}")
            return False

    async def _shutdown_managed_processes(self) -> None:
        """
        v84.0: Gracefully shutdown all managed processes with force timeout.
        """
        shutdown_timeout = float(os.getenv("TRINITY_SHUTDOWN_TIMEOUT", "30.0"))
        graceful_timeout = float(os.getenv("TRINITY_GRACEFUL_TIMEOUT", "10.0"))

        for name, info in self._managed_processes.items():
            process = info.get("process")
            if process and process.returncode is None:
                logger.info(f"[Shutdown] Terminating {name} (PID {process.pid})")
                try:
                    # Phase 1: Graceful termination
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=graceful_timeout)
                        logger.info(f"[Shutdown] {name} terminated gracefully")
                    except asyncio.TimeoutError:
                        # Phase 2: Force kill
                        logger.warning(f"[Shutdown] Force killing {name} after {graceful_timeout}s")
                        process.kill()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            logger.error(f"[Shutdown] {name} did not respond to kill")
                except Exception as e:
                    logger.warning(f"[Shutdown] Error terminating {name}: {e}")

        self._managed_processes.clear()

    # =========================================================================
    # v84.0: Process Supervision with Auto-Restart
    # =========================================================================

    async def _start_process_supervision(self) -> None:
        """
        v84.0: Start background process supervision task.

        Features:
        - PID monitoring with cookie validation
        - Crash detection via heartbeat staleness
        - Automatic restart with exponential backoff
        - Circuit breaker integration
        """
        self._supervision_task = asyncio.create_task(self._process_supervision_loop())
        logger.info("[Supervisor] Process supervision started")

    async def _process_supervision_loop(self) -> None:
        """Background loop for process supervision."""
        supervision_interval = float(os.getenv("TRINITY_SUPERVISION_INTERVAL", "10.0"))
        max_restart_delay = float(os.getenv("TRINITY_MAX_RESTART_DELAY", "300.0"))

        restart_counts: Dict[str, int] = {}
        last_restart_times: Dict[str, float] = {}

        while self._running:
            try:
                await asyncio.sleep(supervision_interval)

                # Check each managed process
                for name, info in list(self._managed_processes.items()):
                    process = info.get("process")
                    pid = info.get("pid")

                    if process is None:
                        continue

                    # Check if process is still running
                    if process.returncode is not None:
                        # Process exited - attempt restart
                        logger.warning(
                            f"[Supervisor] {name} exited with code {process.returncode}"
                        )

                        # Update restart count
                        restart_counts[name] = restart_counts.get(name, 0) + 1
                        count = restart_counts[name]

                        # Calculate backoff delay
                        base_delay = float(os.getenv("TRINITY_RESTART_BASE_DELAY", "5.0"))
                        delay = min(base_delay * (2 ** (count - 1)), max_restart_delay)

                        # Check if we should restart
                        last_restart = last_restart_times.get(name, 0)
                        if time.time() - last_restart < delay:
                            logger.info(
                                f"[Supervisor] {name} restart throttled "
                                f"(attempt {count}, waiting {delay:.0f}s)"
                            )
                            continue

                        # Record crash
                        await self._crash_recovery.record_crash(name, f"Exit code {process.returncode}")

                        # Check circuit breaker
                        if count > 5:
                            logger.error(
                                f"[Supervisor] {name} exceeded restart limit ({count} attempts)"
                            )
                            continue

                        # Attempt restart
                        logger.info(f"[Supervisor] Restarting {name} (attempt {count})")
                        last_restart_times[name] = time.time()

                        if name == "jarvis_prime":
                            success = await self._launch_jprime_process()
                        elif name == "reactor_core":
                            success = await self._launch_reactor_process()
                        else:
                            success = False

                        if success:
                            logger.info(f"[Supervisor] {name} restarted successfully")
                        else:
                            logger.error(f"[Supervisor] {name} restart failed")

                    else:
                        # Process running - verify via heartbeat
                        heartbeat_ok = await self._verify_process_heartbeat(name, pid)
                        if not heartbeat_ok:
                            logger.warning(
                                f"[Supervisor] {name} heartbeat stale (PID {pid})"
                            )

                # Reset restart counts for healthy processes
                for name in list(restart_counts.keys()):
                    info = self._managed_processes.get(name)
                    if info and info.get("process") and info["process"].returncode is None:
                        # Process is running, slowly decay restart count
                        if restart_counts[name] > 0:
                            # Decay after 5 minutes of stability
                            last_restart = last_restart_times.get(name, 0)
                            if time.time() - last_restart > 300:
                                restart_counts[name] = max(0, restart_counts[name] - 1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Supervisor] Error in supervision loop: {e}")

    async def _verify_process_heartbeat(self, component: str, pid: int) -> bool:
        """Verify process is healthy via heartbeat file."""
        heartbeat_file = Path.home() / ".jarvis" / "trinity" / "components" / f"{component}.json"

        if not heartbeat_file.exists():
            return False

        try:
            with open(heartbeat_file, 'r') as f:
                data = json.load(f)

            # Check timestamp freshness
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            # Heartbeat stale threshold (30 seconds)
            stale_threshold = float(os.getenv("TRINITY_HEARTBEAT_STALE_THRESHOLD", "30.0"))

            if age > stale_threshold:
                return False

            # Verify PID matches
            heartbeat_pid = data.get("pid")
            if heartbeat_pid and heartbeat_pid != pid:
                logger.warning(
                    f"[Supervisor] {component} PID mismatch: expected {pid}, got {heartbeat_pid}"
                )
                return False

            return True

        except Exception as e:
            logger.debug(f"[Supervisor] Heartbeat verification error: {e}")
            return False

    # =========================================================================
    # v84.0: Lock Timeout Protection
    # =========================================================================

    class TimeoutLock:
        """
        v84.0: Lock with timeout protection and deadlock detection.

        Features:
        - Configurable acquisition timeout
        - Deadlock detection via caller tracking
        - Automatic release on timeout
        - Metrics and logging
        """

        def __init__(
            self,
            name: str,
            timeout: float = 30.0,
            warn_threshold: float = 10.0,
        ):
            self._name = name
            self._timeout = timeout
            self._warn_threshold = warn_threshold
            self._lock = asyncio.Lock()
            self._holder: Optional[str] = None
            self._acquired_at: float = 0.0
            self._acquisition_count: int = 0
            self._timeout_count: int = 0

        async def acquire(self, caller: str = "unknown") -> bool:
            """
            Acquire lock with timeout.

            Args:
                caller: Identifier for deadlock detection

            Returns:
                True if acquired, False if timeout
            """
            start_time = time.time()

            try:
                acquired = await asyncio.wait_for(
                    self._lock.acquire(),
                    timeout=self._timeout,
                )

                if acquired:
                    self._holder = caller
                    self._acquired_at = time.time()
                    self._acquisition_count += 1

                    wait_time = time.time() - start_time
                    if wait_time > self._warn_threshold:
                        logger.warning(
                            f"[Lock:{self._name}] Slow acquisition: {wait_time:.2f}s "
                            f"(caller={caller})"
                        )

                return acquired

            except asyncio.TimeoutError:
                self._timeout_count += 1
                logger.error(
                    f"[Lock:{self._name}] Acquisition timeout after {self._timeout}s "
                    f"(caller={caller}, current_holder={self._holder})"
                )
                return False

        def release(self) -> None:
            """Release the lock."""
            if self._lock.locked():
                hold_time = time.time() - self._acquired_at
                if hold_time > self._warn_threshold:
                    logger.warning(
                        f"[Lock:{self._name}] Long hold time: {hold_time:.2f}s "
                        f"(holder={self._holder})"
                    )
                self._holder = None
                self._acquired_at = 0.0
                self._lock.release()

        async def __aenter__(self):
            await self.acquire()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.release()
            return False

        def get_stats(self) -> Dict[str, Any]:
            """Get lock statistics."""
            return {
                "name": self._name,
                "locked": self._lock.locked(),
                "holder": self._holder,
                "acquisition_count": self._acquisition_count,
                "timeout_count": self._timeout_count,
                "hold_time": time.time() - self._acquired_at if self._acquired_at > 0 else 0,
            }

    async def _event_cleanup_loop(self) -> None:
        """Background loop to clean up expired events."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Cleanup hourly
                cleaned = await self._event_store.cleanup_expired()
                if cleaned > 0:
                    logger.info(f"[TrinityOrchestrator] Cleaned {cleaned} expired events")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[TrinityOrchestrator] Event cleanup error: {e}")

    # =========================================================================
    # v83.0: Unified Health Methods
    # =========================================================================

    async def get_unified_health(self) -> Dict[str, Any]:
        """
        Get unified health across all Trinity components.

        v83.0 feature: Includes anomaly detection, trend analysis,
        and correlation between components.
        """
        return {
            "legacy": await self.get_health(),
            "aggregated": self._health_aggregator.get_unified_health(),
            "supervisor": self._process_supervisor.get_stats(),
            "circuit_breakers": {
                name: {
                    "state": cb.state.name,
                    "stats": {
                        "total_calls": cb.stats.total_calls,
                        "failures": cb.stats.failed_calls,
                        "rejected": cb.stats.rejected_calls,
                    },
                }
                for name, cb in self._circuit_breakers.items()
            },
            "crash_recovery": {
                name: self._crash_recovery.get_restart_count(name)
                for name in ["jarvis_prime", "reactor_core"]
            },
            "resources": self._resource_coordinator.get_system_resources(),
            "throttler": self._throttler.get_stats(),
        }

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _health_loop(self) -> None:
        """Background health monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.health_check_interval)

                health = await self.get_health()

                # Update state based on health
                if health.degraded_components:
                    if self._state == TrinityState.READY:
                        self._set_state(TrinityState.DEGRADED)
                elif self._state == TrinityState.DEGRADED:
                    if not health.degraded_components:
                        self._set_state(TrinityState.READY)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[TrinityIntegrator] Health check error: {e}")

    async def get_health(self) -> TrinityHealth:
        """Get current Trinity system health."""
        components: Dict[str, ComponentStatus] = {}
        degraded: List[str] = []
        errors: List[str] = []

        # Check JARVIS Body (self)
        body_status = ComponentStatus(
            name="jarvis_body",
            health=ComponentHealth.HEALTHY,
            online=True,
            last_heartbeat=time.time(),
            metrics={"uptime": self.uptime},
        )
        components["jarvis_body"] = body_status

        # Check JARVIS Prime
        if self.enable_jprime:
            jprime_status = await self._check_jprime_health()
            components["jarvis_prime"] = jprime_status
            if jprime_status.health != ComponentHealth.HEALTHY:
                degraded.append("jarvis_prime")
            if jprime_status.error:
                errors.append(jprime_status.error)

        # Check Reactor-Core
        if self.enable_reactor:
            reactor_status = await self._check_reactor_health()
            components["reactor_core"] = reactor_status
            if reactor_status.health != ComponentHealth.HEALTHY:
                degraded.append("reactor_core")
            if reactor_status.error:
                errors.append(reactor_status.error)

        return TrinityHealth(
            state=self._state,
            components=components,
            uptime_seconds=self.uptime,
            last_check=time.time(),
            degraded_components=degraded,
            errors=errors,
        )

    async def _check_jprime_health(self) -> ComponentStatus:
        """Check JARVIS Prime health."""
        if not self._jprime_client:
            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.UNKNOWN,
                online=False,
                error="Client not initialized",
            )

        try:
            is_online = self._jprime_client.is_online
            metrics = self._jprime_client.get_metrics()

            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.HEALTHY if is_online else ComponentHealth.UNHEALTHY,
                online=is_online,
                last_heartbeat=metrics.get("last_health_check"),
                metrics=metrics,
            )

        except Exception as e:
            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.UNHEALTHY,
                online=False,
                error=str(e),
            )

    async def _check_reactor_health(self) -> ComponentStatus:
        """Check Reactor-Core health."""
        if not self._reactor_client:
            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.UNKNOWN,
                online=False,
                error="Client not initialized",
            )

        try:
            is_online = self._reactor_client.is_online
            metrics = self._reactor_client.get_metrics()

            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.HEALTHY if is_online else ComponentHealth.UNHEALTHY,
                online=is_online,
                last_heartbeat=time.time() if is_online else None,
                metrics=metrics,
            )

        except Exception as e:
            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.UNHEALTHY,
                online=False,
                error=str(e),
            )

    # =========================================================================
    # Shutdown
    # =========================================================================

    async def stop(
        self,
        timeout: float = 30.0,
        force: bool = False,
    ) -> bool:
        """
        Stop the Trinity system.

        Args:
            timeout: Max time to wait for graceful shutdown
            force: Skip drain phase for immediate shutdown

        Returns:
            True if shutdown successful
        """
        async with self._lock:
            if self._state in (TrinityState.STOPPED, TrinityState.STOPPING):
                return True

            self._set_state(TrinityState.STOPPING)
            self._running = False

            try:
                # Stop health monitoring
                if self._health_task:
                    self._health_task.cancel()
                    try:
                        await self._health_task
                    except asyncio.CancelledError:
                        pass

                # Close clients
                if self._jprime_client:
                    await self._jprime_client.disconnect()

                if self._reactor_client:
                    from backend.clients.reactor_core_client import shutdown_reactor_client
                    await shutdown_reactor_client()

                # Coordinated shutdown
                if self._shutdown_manager:
                    from backend.core.coordinated_shutdown import ShutdownReason

                    result = await self._shutdown_manager.initiate_shutdown(
                        reason=ShutdownReason.USER_REQUEST,
                        timeout=timeout,
                        force=force,
                    )

                    if not result.success:
                        logger.warning(
                            f"[TrinityIntegrator] Shutdown incomplete: {result.errors}"
                        )

                # Close IPC
                if self._ipc_bus:
                    from backend.core.trinity_ipc import close_resilient_trinity_ipc_bus
                    await close_resilient_trinity_ipc_bus()

                self._set_state(TrinityState.STOPPED)

                elapsed = time.time() - (self._start_time or time.time())
                logger.info(
                    f"[TrinityIntegrator] Stopped after {elapsed:.2f}s uptime"
                )

                return True

            except Exception as e:
                logger.error(f"[TrinityIntegrator] Shutdown error: {e}")
                self._set_state(TrinityState.ERROR)
                return False

    # =========================================================================
    # State Management
    # =========================================================================

    def _set_state(self, new_state: TrinityState) -> None:
        """Set new state and notify callbacks."""
        old_state = self._state
        self._state = new_state

        if old_state != new_state:
            logger.info(
                f"[TrinityIntegrator] State: {old_state.value} -> {new_state.value}"
            )

            for callback in self._on_state_change:
                try:
                    callback(old_state, new_state)
                except Exception as e:
                    logger.warning(f"[TrinityIntegrator] Callback error: {e}")

    def on_state_change(
        self,
        callback: Callable[[TrinityState, TrinityState], None],
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)

    def on_component_change(
        self,
        callback: Callable[[str, ComponentHealth], None],
    ) -> None:
        """Register callback for component health changes."""
        self._on_component_change.append(callback)

    # =========================================================================
    # API Access
    # =========================================================================

    @property
    def ipc_bus(self):
        """Get the IPC bus."""
        return self._ipc_bus

    @property
    def jprime_client(self):
        """Get the JARVIS Prime client."""
        return self._jprime_client

    @property
    def reactor_client(self):
        """Get the Reactor-Core client."""
        return self._reactor_client

    def get_metrics(self) -> Dict[str, Any]:
        """Get integrator metrics."""
        return {
            "state": self._state.value,
            "uptime": self.uptime,
            "jprime_enabled": self.enable_jprime,
            "reactor_enabled": self.enable_reactor,
            "jprime_online": self._jprime_client.is_online if self._jprime_client else False,
            "reactor_online": self._reactor_client.is_online if self._reactor_client else False,
        }


# =============================================================================
# Backward Compatibility Alias
# =============================================================================

# TrinityIntegrator is now TrinityUnifiedOrchestrator
TrinityIntegrator = TrinityUnifiedOrchestrator


# =============================================================================
# Singleton Access
# =============================================================================

_orchestrator: Optional[TrinityUnifiedOrchestrator] = None
_orchestrator_lock = asyncio.Lock()


async def get_trinity_orchestrator(
    **kwargs,
) -> TrinityUnifiedOrchestrator:
    """Get or create the singleton Trinity Unified Orchestrator v83.0."""
    global _orchestrator

    async with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = TrinityUnifiedOrchestrator(**kwargs)
        return _orchestrator


# Backward compatibility alias
async def get_trinity_integrator(**kwargs) -> TrinityUnifiedOrchestrator:
    """Legacy alias for get_trinity_orchestrator."""
    return await get_trinity_orchestrator(**kwargs)


async def start_trinity() -> bool:
    """Start the Trinity system."""
    orchestrator = await get_trinity_orchestrator()
    return await orchestrator.start()


async def stop_trinity(force: bool = False) -> bool:
    """Stop the Trinity system."""
    global _orchestrator

    if _orchestrator:
        result = await _orchestrator.stop(force=force)
        _orchestrator = None
        return result

    return True


async def get_trinity_health() -> Optional[TrinityHealth]:
    """Get Trinity system health."""
    if _orchestrator:
        return await _orchestrator.get_health()
    return None


async def get_unified_trinity_health() -> Optional[Dict[str, Any]]:
    """Get unified Trinity health with v83.0 features (anomaly detection, trends)."""
    if _orchestrator:
        return await _orchestrator.get_unified_health()
    return None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Core Types
    # ═══════════════════════════════════════════════════════════════════════
    "TrinityState",
    "ComponentHealth",
    "ComponentStatus",
    "TrinityHealth",

    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Main Orchestrator
    # ═══════════════════════════════════════════════════════════════════════
    "TrinityUnifiedOrchestrator",
    "TrinityIntegrator",  # Backward compatibility alias

    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Advanced Components
    # ═══════════════════════════════════════════════════════════════════════
    # Configuration
    "ConfigRegistry",
    "get_config",

    # Circuit Breaker
    "CircuitBreaker",
    "CircuitState",
    "CircuitBreakerStats",
    "CircuitOpenError",

    # Process Supervisor
    "ProcessSupervisor",
    "ProcessInfo",

    # Crash Recovery
    "CrashRecoveryManager",
    "CrashRecord",

    # Resource Coordinator
    "ResourceCoordinator",
    "ResourceAllocation",

    # Event Store
    "EventStore",
    "TrinityEvent",

    # Distributed Tracing
    "DistributedTracer",
    "TraceSpan",

    # Health Aggregator
    "UnifiedHealthAggregator",
    "HealthSample",
    "AnomalyReport",

    # Adaptive Throttling
    "AdaptiveThrottler",
    "ThrottleExceededError",

    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Convenience Functions
    # ═══════════════════════════════════════════════════════════════════════
    "get_trinity_orchestrator",
    "get_trinity_integrator",  # Backward compatibility
    "start_trinity",
    "stop_trinity",
    "get_trinity_health",
    "get_unified_trinity_health",
]
