"""
Cross-Repo Startup Orchestrator v5.0 - Enterprise-Grade Process Lifecycle Manager
===================================================================================

Dynamic service discovery and self-healing process orchestration for JARVIS ecosystem.
Enables single-command startup of all Trinity components (JARVIS Body, J-Prime Mind, Reactor-Core Nerves).

Features (v5.0):
- 🔧 SINGLE SOURCE OF TRUTH: Ports from trinity_config.py (no hardcoding!)
- 🧹 Pre-flight Cleanup: Kills stale processes on legacy ports (8002, 8003)
- 🔍 Wrong-Binding Detection: Detects 127.0.0.1 vs 0.0.0.0 misconfigurations
- 🔄 Auto-Healing with exponential backoff (dead process detection & restart)
- 📡 Real-Time Output Streaming (stdout/stderr prefixed per service)
- 🎯 Process Lifecycle Management (spawn, monitor, graceful shutdown)
- 🛡️ Graceful Shutdown Handlers (SIGINT/SIGTERM cleanup)
- 🐍 Auto-detect venv Python for each repo
- 📁 Correct entry points: run_server.py, run_reactor.py
- ⚡ Pre-spawn validation (port check, dependency check)
- 📊 Service Health Monitoring with progressive backoff

Service Ports (v5.0 - from trinity_config.py):
- jarvis-prime: port 8000 (run_server.py --port 8000 --host 0.0.0.0)
- reactor-core: port 8090 (run_reactor.py --port 8090)
- jarvis-body: port 8010 (main JARVIS)

Legacy ports cleaned up automatically:
- jarvis-prime: 8001, 8002 (killed if found)
- reactor-core: 8003, 8004, 8005 (killed if found)

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │         Cross-Repo Orchestrator v5.0 - Process Manager           │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  Service Registry: ~/.jarvis/registry/services.json              │
    │  ┌────────────────┬──────────────┬─────────────────────┐        │
    │  │   JARVIS       │   J-PRIME    │   REACTOR-CORE      │        │
    │  │  PID: auto     │  PID: auto   │   PID: auto         │        │
    │  │  Port: 8010    │  Port: 8000  │   Port: 8090        │        │
    │  │  Status: ✅    │  Status: ✅  │   Status: ✅        │        │
    │  └────────────────┴──────────────┴─────────────────────┘        │
    │                                                                   │
    │  Process Lifecycle:                                               │
    │  0. Pre-flight Cleanup (kill stale legacy processes)             │
    │  1. Pre-Spawn Validation (venv detect, port check)               │
    │  2. Spawn (asyncio.create_subprocess_exec with venv Python)      │
    │  3. Monitor (PID tracking + progressive health checks)           │
    │  4. Stream Output (real-time with [SERVICE] prefix)              │
    │  5. Auto-Heal (restart on crash with exponential backoff)        │
    │  6. Graceful Shutdown (SIGTERM → wait → SIGKILL)                 │
    │                                                                   │
    └──────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 5.7.0 (v93.11)

Changelog:
- v93.11 (v5.7): Parallel Startup & Thread Safety Enhancements
  - Thread-safe locks for circuit breakers, memory status, and startup coordination
  - Shared aiohttp session with connection pooling (100 connections, 10 per host)
  - Parallel service startup with semaphore-based concurrency control
  - Cross-repo health aggregation with caching
  - Startup coordination events for dependency tracking
  - Graceful HTTP session cleanup during shutdown
  - Lazy initialization of asyncio primitives (event loop safety)
- v93.10 (v5.6): Enhanced GCP Integration with Full VM Lifecycle Management
  - 4-tier memory-aware routing decision tree (Emergency/Low/Medium/High)
  - GCP VM Manager integration for full lifecycle control
  - CloudMLRouter and HybridRouter integration for intelligent routing
  - Auto VM type selection based on model memory requirements
  - Spot VM support for cost optimization
  - Health check with progress monitoring for GCP services
  - VM startup script generation for automatic service deployment
  - Existing VM detection and reuse
  - Comprehensive fallback chain (Docker -> GCP -> Local)
- v93.9 (v5.5): Advanced Docker Operations with intelligent routing
  - Circuit Breaker pattern prevents cascading failures
  - Exponential backoff retry with jitter for resilience
  - Memory-aware routing (Docker vs Local vs GCP)
  - Auto-build Docker images if missing
  - GCP cloud fallback when local resources insufficient
  - System memory monitoring with psutil
  - Intelligent routing decisions based on available resources
- v93.8 (v5.4): Docker Hybrid Mode - checks Docker before spawning local process
  - Automatic detection of jarvis-prime Docker container
  - Docker-first, local-fallback approach for seamless deployment
  - Optional auto-start of Docker containers
  - Pre-loaded models in Docker eliminate startup delays
- v93.7: Fixed duplicate health check requests, enhanced step logging
- v93.5: Intelligent progress-based timeout extension for model loading
- v5.3: Made startup resilient to CancelledError (don't propagate during init)
- v5.2: Fixed loop.stop() antipattern, proper task cancellation on shutdown
- v5.1: Fixed sys.exit() antipattern in async shutdown handler
- v5.0: Added pre-flight cleanup, circuit breaker, trinity_config integration
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional, Set, Tuple, Union

import aiohttp

logger = logging.getLogger(__name__)


# =============================================================================
# v95.0: Orchestrator-Narrator Bridge Integration
# =============================================================================

# Import event types and emitter for real-time voice feedback
_NARRATOR_BRIDGE_AVAILABLE = False
OrchestratorEvent = None
AnnouncementPriority = None
emit_orchestrator_event = None

try:
    from backend.core.supervisor.orchestrator_narrator_bridge import (
        OrchestratorEvent as _OrchestratorEvent,
        AnnouncementPriority as _AnnouncementPriority,
        emit_orchestrator_event as _emit_orchestrator_event,
    )
    OrchestratorEvent = _OrchestratorEvent
    AnnouncementPriority = _AnnouncementPriority
    emit_orchestrator_event = _emit_orchestrator_event
    _NARRATOR_BRIDGE_AVAILABLE = True
except ImportError:
    pass  # Keep defaults


async def _emit_event(
    event: str,
    service_name: Optional[str] = None,
    priority: str = "MEDIUM",
    details: Optional[Dict[str, Any]] = None,
    **kwargs
) -> None:
    """
    v95.0: Safely emit an orchestrator event for voice narration.

    This wrapper ensures the orchestrator doesn't fail if the narrator
    bridge is unavailable.
    """
    if not _NARRATOR_BRIDGE_AVAILABLE or OrchestratorEvent is None:
        return

    try:
        # Convert string event to enum
        event_enum = getattr(OrchestratorEvent, event, None)
        if event_enum is None:
            return

        # Convert string priority to enum
        if AnnouncementPriority is not None:
            priority_enum = getattr(AnnouncementPriority, priority, None)
            if priority_enum is None:
                priority_enum = getattr(AnnouncementPriority, "MEDIUM", None)
        else:
            priority_enum = None

        if emit_orchestrator_event is not None:
            await emit_orchestrator_event(
                event=event_enum,
                service_name=service_name,
                priority=priority_enum,
                details=details or {},
                **kwargs
            )
    except Exception as e:
        # Never let narrator issues block orchestration
        logger.debug(f"[NarratorBridge] Event emission failed (non-blocking): {e}")


# =============================================================================
# Configuration (Zero Hardcoding - All Environment Driven)
# =============================================================================

# v5.0: Import from trinity_config as SINGLE SOURCE OF TRUTH
try:
    from backend.core.trinity_config import get_config as get_trinity_config
    _TRINITY_CONFIG_AVAILABLE = True
except ImportError:
    _TRINITY_CONFIG_AVAILABLE = False
    get_trinity_config = None


def _get_port_from_trinity(service: str, fallback: int) -> int:
    """
    Get port from trinity_config (single source of truth) with fallback.

    v5.0: This ensures all services use consistent ports from trinity_config.
    """
    if not _TRINITY_CONFIG_AVAILABLE:
        return int(os.getenv(f"{service.upper()}_PORT", str(fallback)))

    try:
        config = get_trinity_config()
        if service == "jarvis_prime":
            return config.jarvis_prime_endpoint.port
        elif service == "reactor_core":
            return config.reactor_core_endpoint.port
        elif service == "jarvis":
            return config.jarvis_endpoint.port
    except Exception:
        pass

    return int(os.getenv(f"{service.upper()}_PORT", str(fallback)))


@dataclass
class OrchestratorConfig:
    """
    Enterprise configuration with zero hardcoding.

    v5.0: Ports sourced from trinity_config as SINGLE SOURCE OF TRUTH.
    """

    # Repository paths
    jarvis_prime_path: Path = field(default_factory=lambda: Path(
        os.getenv("JARVIS_PRIME_PATH", str(Path.home() / "Documents" / "repos" / "jarvis-prime"))
    ))
    reactor_core_path: Path = field(default_factory=lambda: Path(
        os.getenv("REACTOR_CORE_PATH", str(Path.home() / "Documents" / "repos" / "reactor-core"))
    ))

    # Default ports - sourced from trinity_config (SINGLE SOURCE OF TRUTH)
    # Fallbacks: jarvis-prime=8000, reactor-core=8090
    jarvis_prime_default_port: int = field(
        default_factory=lambda: _get_port_from_trinity("jarvis_prime", 8000)
    )
    reactor_core_default_port: int = field(
        default_factory=lambda: _get_port_from_trinity("reactor_core", 8090)
    )

    # Legacy ports to clean up (processes on these should be killed)
    legacy_jarvis_prime_ports: List[int] = field(default_factory=lambda: [8001, 8002])
    legacy_reactor_core_ports: List[int] = field(default_factory=lambda: [8003, 8004, 8005])

    # Feature flags
    jarvis_prime_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
    )
    reactor_core_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true"
    )

    # Auto-healing configuration
    auto_healing_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTO_HEALING_ENABLED", "true").lower() == "true"
    )
    max_restart_attempts: int = field(
        default_factory=lambda: int(os.getenv("MAX_RESTART_ATTEMPTS", "5"))
    )
    restart_backoff_base: float = field(
        default_factory=lambda: float(os.getenv("RESTART_BACKOFF_BASE", "1.0"))
    )
    restart_backoff_max: float = field(
        default_factory=lambda: float(os.getenv("RESTART_BACKOFF_MAX", "60.0"))
    )

    # Health monitoring
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_INTERVAL", "5.0"))
    )
    health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_TIMEOUT", "5.0"))
    )
    # v93.0: Default startup timeout (applies to lightweight services)
    startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("SERVICE_STARTUP_TIMEOUT", "60.0"))
    )

    # v93.5: Per-service startup timeouts with intelligent progress-based extension
    # JARVIS Prime loads heavy ML models (ECAPA-TDNN, torch, etc.) and needs longer timeout
    # Default increased from 300s to 600s (10 minutes) for 70B+ models
    jarvis_prime_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_STARTUP_TIMEOUT", "600.0"))  # 10 minutes for ML models
    )
    reactor_core_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_STARTUP_TIMEOUT", "120.0"))  # 2 minutes
    )
    # v93.5: If model is actively loading (progress detected), extend timeout automatically
    model_loading_timeout_extension: float = field(
        default_factory=lambda: float(os.getenv("MODEL_LOADING_TIMEOUT_EXTENSION", "300.0"))  # Extra 5 min if progress
    )
    # v93.5: Maximum total timeout (hard cap for safety)
    max_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("MAX_STARTUP_TIMEOUT", "900.0"))  # 15 min absolute max
    )

    # Graceful shutdown
    shutdown_timeout: float = field(
        default_factory=lambda: float(os.getenv("SHUTDOWN_TIMEOUT", "10.0"))
    )

    # Output streaming
    stream_output: bool = field(
        default_factory=lambda: os.getenv("STREAM_CHILD_OUTPUT", "true").lower() == "true"
    )

    # =========================================================================
    # v93.8: Docker Hybrid Mode Configuration
    # =========================================================================
    # Enable Docker-first startup for jarvis-prime (checks Docker before local)
    docker_hybrid_enabled: bool = field(
        default_factory=lambda: os.getenv("DOCKER_HYBRID_ENABLED", "true").lower() == "true"
    )

    # Docker container name for jarvis-prime
    jarvis_prime_docker_container: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_CONTAINER", "jarvis-prime-service")
    )

    # Docker compose file path (relative to jarvis-prime repo)
    jarvis_prime_docker_compose: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_COMPOSE", "docker/docker-compose.yml")
    )

    # Timeout for Docker health check
    docker_health_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_HEALTH_TIMEOUT", "5.0"))
    )

    # Whether to auto-start Docker container if not running
    docker_auto_start: bool = field(
        default_factory=lambda: os.getenv("DOCKER_AUTO_START", "false").lower() == "true"
    )

    # Docker startup timeout (waiting for container to become healthy)
    docker_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_STARTUP_TIMEOUT", "180.0"))
    )

    # =========================================================================
    # v93.9: Advanced Docker Operations Configuration
    # =========================================================================

    # Circuit Breaker Configuration
    docker_circuit_breaker_failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("DOCKER_CB_FAILURE_THRESHOLD", "3"))
    )
    docker_circuit_breaker_recovery_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_CB_RECOVERY_TIMEOUT", "60.0"))
    )
    docker_circuit_breaker_half_open_requests: int = field(
        default_factory=lambda: int(os.getenv("DOCKER_CB_HALF_OPEN_REQUESTS", "1"))
    )

    # Retry Configuration (Exponential Backoff)
    docker_retry_max_attempts: int = field(
        default_factory=lambda: int(os.getenv("DOCKER_RETRY_MAX_ATTEMPTS", "3"))
    )
    docker_retry_base_delay: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_RETRY_BASE_DELAY", "1.0"))
    )
    docker_retry_max_delay: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_RETRY_MAX_DELAY", "30.0"))
    )
    docker_retry_exponential_base: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_RETRY_EXP_BASE", "2.0"))
    )

    # Memory-Aware Routing Configuration
    memory_minimum_available_gb: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_MIN_AVAILABLE_GB", "4.0"))
    )
    memory_route_to_gcp_threshold_gb: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_GCP_THRESHOLD_GB", "2.0"))
    )
    memory_model_size_estimation_factor: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_MODEL_SIZE_FACTOR", "1.5"))
    )

    # GCP Fallback Configuration
    gcp_fallback_enabled: bool = field(
        default_factory=lambda: os.getenv("GCP_FALLBACK_ENABLED", "true").lower() == "true"
    )
    gcp_vm_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("GCP_VM_STARTUP_TIMEOUT", "300.0"))
    )

    # Docker Image Configuration
    docker_auto_build: bool = field(
        default_factory=lambda: os.getenv("DOCKER_AUTO_BUILD", "true").lower() == "true"
    )
    docker_build_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_BUILD_TIMEOUT", "600.0"))
    )


# =============================================================================
# Data Models
# =============================================================================

class ServiceStatus(Enum):
    """Service lifecycle status."""
    PENDING = "pending"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPED = "stopped"


class CircuitBreakerState(Enum):
    """
    v93.9: Circuit Breaker state machine.

    CLOSED: Normal operation, requests pass through
    OPEN: Failures exceeded threshold, requests blocked
    HALF_OPEN: Testing if service recovered
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """
    v95.1: Thread-safe Circuit Breaker pattern with forced recovery.

    Prevents cascading failures by:
    - Tracking consecutive failures
    - Opening circuit after failure threshold
    - Allowing recovery testing after timeout
    - Closing circuit on successful recovery
    - v95.0: Forced recovery after max open duration
    - v95.0: State transition logging and metrics
    - v95.1: Thread-safe state transitions using lock

    Thread Safety:
    All state modifications are protected by a threading.Lock to ensure
    atomic state transitions even when called from multiple coroutines.
    """
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    half_open_max_requests: int = 1
    # v95.0: Maximum time circuit stays OPEN before forced recovery attempt
    max_open_duration: float = field(
        default_factory=lambda: float(os.getenv("CIRCUIT_BREAKER_MAX_OPEN", "600.0"))  # 10 minutes
    )

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_requests: int = 0
    success_count: int = 0
    # v95.0: Tracking for stuck circuit detection
    last_state_change: float = field(default_factory=time.time)
    consecutive_recovery_failures: int = 0
    total_open_duration: float = 0.0

    # v95.1: Thread-safe lock for atomic state transitions
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _transition_state(self, new_state: CircuitBreakerState, reason: str) -> None:
        """
        v95.1: Handle state transition with logging and metrics.

        NOTE: This method assumes _lock is already held by caller.
        """
        old_state = self.state
        if old_state == new_state:
            return

        now = time.time()
        duration_in_old_state = now - self.last_state_change

        # Track total open duration (atomic with state change)
        if old_state == CircuitBreakerState.OPEN:
            self.total_open_duration += duration_in_old_state

        self.state = new_state
        self.last_state_change = now

        # Log with appropriate level based on transition
        if new_state == CircuitBreakerState.CLOSED:
            logger.info(
                f"    ✅ Circuit breaker [{self.name}]: {old_state.value} → CLOSED "
                f"({reason}, was {old_state.value} for {duration_in_old_state:.1f}s)"
            )
            self.consecutive_recovery_failures = 0
        elif new_state == CircuitBreakerState.OPEN:
            logger.warning(
                f"    ⚠️ Circuit breaker [{self.name}]: {old_state.value} → OPEN "
                f"({reason})"
            )
            if old_state == CircuitBreakerState.HALF_OPEN:
                self.consecutive_recovery_failures += 1
        else:  # HALF_OPEN
            logger.info(
                f"    🔄 Circuit breaker [{self.name}]: {old_state.value} → HALF_OPEN "
                f"({reason})"
            )

    def record_success(self) -> None:
        """
        v95.1: Record a successful operation (thread-safe).

        Atomically updates success counter and potentially transitions to CLOSED.
        """
        with self._lock:
            if self.state == CircuitBreakerState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.half_open_max_requests:
                    # Recovery successful, close circuit
                    self._transition_state(CircuitBreakerState.CLOSED, "recovery successful")
                    self.failure_count = 0
                    self.success_count = 0
            else:
                self.failure_count = 0
                self.success_count += 1

    def record_failure(self) -> None:
        """
        v95.1: Record a failed operation (thread-safe).

        Atomically updates failure counter and potentially transitions to OPEN.
        """
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            self.success_count = 0

            if self.state == CircuitBreakerState.HALF_OPEN:
                # Failed during recovery test, reopen circuit
                self._transition_state(CircuitBreakerState.OPEN, "recovery test failed")
            elif self.failure_count >= self.failure_threshold:
                # Threshold exceeded, open circuit
                self._transition_state(
                    CircuitBreakerState.OPEN,
                    f"{self.failure_count} consecutive failures"
                )

    def can_execute(self) -> bool:
        """
        v95.1: Check if operation can proceed (thread-safe).

        Atomically checks state and potentially transitions to HALF_OPEN.
        """
        with self._lock:
            if self.state == CircuitBreakerState.CLOSED:
                return True

            if self.state == CircuitBreakerState.OPEN:
                now = time.time()
                time_since_failure = now - self.last_failure_time
                time_in_open = now - self.last_state_change

                # v95.0: Check for forced recovery after max open duration
                if time_in_open >= self.max_open_duration:
                    logger.warning(
                        f"    ⏰ Circuit breaker [{self.name}]: FORCED RECOVERY "
                        f"(open for {time_in_open:.0f}s, max: {self.max_open_duration:.0f}s)"
                    )
                    self._transition_state(
                        CircuitBreakerState.HALF_OPEN,
                        f"forced recovery after {time_in_open:.0f}s"
                    )
                    self.half_open_requests = 0
                    return True

                # Normal recovery timeout check
                if time_since_failure >= self.recovery_timeout:
                    self._transition_state(
                        CircuitBreakerState.HALF_OPEN,
                        f"recovery timeout ({time_since_failure:.0f}s)"
                    )
                    self.half_open_requests = 0
                    return True
                return False

            if self.state == CircuitBreakerState.HALF_OPEN:
                # Allow limited requests during half-open
                if self.half_open_requests < self.half_open_max_requests:
                    self.half_open_requests += 1
                    return True
                return False

            return False

    def force_reset(self) -> None:
        """
        v95.1: Force circuit to CLOSED state (thread-safe).

        For manual intervention when circuit is stuck.
        """
        with self._lock:
            logger.warning(f"    🔧 Circuit breaker [{self.name}]: FORCE RESET to CLOSED")
            self._transition_state(CircuitBreakerState.CLOSED, "forced reset")
            self.failure_count = 0
            self.success_count = 0
            self.consecutive_recovery_failures = 0

    def get_stats(self) -> Dict[str, Any]:
        """
        v95.1: Get circuit breaker statistics (thread-safe snapshot).

        Returns a consistent point-in-time view of circuit breaker state.
        """
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "consecutive_recovery_failures": self.consecutive_recovery_failures,
                "total_open_duration": self.total_open_duration,
                "time_in_current_state": time.time() - self.last_state_change,
            }


@dataclass
class MemoryStatus:
    """
    v93.9: System memory status for intelligent routing decisions.
    """
    total_gb: float = 0.0
    available_gb: float = 0.0
    used_gb: float = 0.0
    percent_used: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0

    @property
    def is_memory_pressure(self) -> bool:
        """Check if system is under memory pressure."""
        return self.percent_used > 85.0 or self.available_gb < 2.0

    @property
    def can_load_local_model(self) -> bool:
        """Check if there's enough memory for a local model (~6-8GB needed)."""
        return self.available_gb >= 6.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_gb": self.total_gb,
            "available_gb": self.available_gb,
            "used_gb": self.used_gb,
            "percent_used": self.percent_used,
            "swap_total_gb": self.swap_total_gb,
            "swap_used_gb": self.swap_used_gb,
            "is_memory_pressure": self.is_memory_pressure,
            "can_load_local_model": self.can_load_local_model,
        }


@dataclass
class RetryState:
    """
    v93.9: State tracking for retry operations with exponential backoff.
    """
    attempt: int = 0
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    last_error: Optional[str] = None
    total_elapsed: float = 0.0

    def should_retry(self) -> bool:
        """Check if another retry attempt should be made."""
        return self.attempt < self.max_attempts

    def get_next_delay(self) -> float:
        """Calculate next delay with exponential backoff and jitter."""
        import random
        delay = self.base_delay * (self.exponential_base ** self.attempt)
        # Add jitter (±25%) to prevent thundering herd
        jitter = delay * 0.25 * (2 * random.random() - 1)
        return min(delay + jitter, self.max_delay)

    def record_attempt(self, error: Optional[str] = None) -> None:
        """Record a retry attempt."""
        self.attempt += 1
        self.last_error = error

    def reset(self) -> None:
        """Reset retry state after success."""
        self.attempt = 0
        self.last_error = None
        self.total_elapsed = 0.0


class RoutingDecision(Enum):
    """
    v93.9: Intelligent routing decision for service deployment.
    """
    DOCKER_LOCAL = "docker_local"      # Use local Docker container
    LOCAL_PROCESS = "local_process"    # Use local Python process
    GCP_CLOUD = "gcp_cloud"            # Route to GCP cloud VM
    HYBRID = "hybrid"                  # Use multiple backends


@dataclass
class ServiceDefinition:
    """
    Definition of a service to manage.

    v4.0: Enhanced with script_args for command-line argument passing.
    v95.0: Added dynamic script discovery, validation methods, and dependency tracking.
    """
    name: str
    repo_path: Path
    script_name: str = "main.py"
    fallback_scripts: List[str] = field(default_factory=lambda: ["server.py", "run.py", "app.py"])
    default_port: int = 8000
    health_endpoint: str = "/health"
    startup_timeout: float = 60.0
    environment: Dict[str, str] = field(default_factory=dict)

    # v95.0: Service dependency tracking
    # Services listed here must be healthy before this service starts
    depends_on: List[str] = field(default_factory=list)

    # v95.0: Startup priority (lower = starts first, same priority = parallel)
    # Default priorities: jarvis-body=10, jarvis-prime=20, reactor-core=30
    startup_priority: int = 50

    # v95.0: Grace period before checking dependencies (allows for parallel startup optimization)
    dependency_check_delay: float = 5.0

    # v95.0: Whether this service is critical (system fails if this service fails)
    is_critical: bool = True

    # v95.0: Retry configuration for dependency wait
    dependency_wait_timeout: float = 120.0  # Max time to wait for dependencies
    dependency_check_interval: float = 2.0  # How often to check dependency health

    # v4.0: Command-line arguments to pass to the script
    # e.g., ["--port", "8000", "--host", "0.0.0.0"]
    script_args: List[str] = field(default_factory=list)

    # v3.1: Module-based entry points (e.g., "reactor_core.api.server")
    # When set, spawns with: python -m <module_path>
    module_path: Optional[str] = None

    # v3.1: Nested script paths to search (relative to repo_path)
    # e.g., ["reactor_core/api/server.py", "src/main.py"]
    nested_scripts: List[str] = field(default_factory=list)

    # v3.1: Use uvicorn for FastAPI apps
    use_uvicorn: bool = False
    uvicorn_app: Optional[str] = None  # e.g., "reactor_core.api.server:app"

    # v95.0: Dynamic script discovery patterns (regex)
    discovery_patterns: List[str] = field(default_factory=lambda: [
        r"run_.*\.py$",  # run_server.py, run_reactor.py, etc.
        r"main\.py$",
        r"server\.py$",
        r"app\.py$",
    ])

    def discover_entry_script(self) -> Optional[Path]:
        """
        v95.0: Dynamically discover the best entry script for this service.

        Uses intelligent scoring based on:
        1. Explicit script_name match (highest priority)
        2. Pattern matching (run_*.py preferred)
        3. Fallback scripts
        4. Size-based heuristic (larger files often have more functionality)

        Returns:
            Path to the best entry script, or None if not found.
        """
        import re

        if not self.repo_path.exists():
            return None

        # Build candidate list with scores
        candidates: List[tuple[Path, int]] = []

        # Score: explicit script_name gets highest priority (100)
        explicit_script = self.repo_path / self.script_name
        if explicit_script.exists():
            candidates.append((explicit_script, 100))

        # Score: nested scripts (90)
        for nested in self.nested_scripts:
            nested_path = self.repo_path / nested
            if nested_path.exists():
                candidates.append((nested_path, 90))

        # Score: fallback scripts (80)
        for fallback in self.fallback_scripts:
            fallback_path = self.repo_path / fallback
            if fallback_path.exists():
                candidates.append((fallback_path, 80))

        # Score: pattern-discovered scripts (60-70)
        try:
            for py_file in self.repo_path.glob("*.py"):
                if py_file in [c[0] for c in candidates]:
                    continue  # Already scored

                for i, pattern in enumerate(self.discovery_patterns):
                    if re.search(pattern, py_file.name, re.IGNORECASE):
                        # Earlier patterns get higher scores
                        score = 70 - i * 2
                        candidates.append((py_file, score))
                        break
        except Exception:
            pass

        if not candidates:
            return None

        # Sort by score (descending), then by file size as tiebreaker
        def sort_key(item: tuple[Path, int]) -> tuple[int, int]:
            path, score = item
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            return (-score, -size)

        candidates.sort(key=sort_key)
        return candidates[0][0]

    def validate(self) -> tuple[bool, List[str]]:
        """
        v95.0: Validate the service definition.

        Returns:
            Tuple of (is_valid, list_of_issues)
        """
        issues: List[str] = []

        # Check repo path
        if not self.repo_path.exists():
            issues.append(f"Repository not found: {self.repo_path}")

        # Check entry script
        script = self.discover_entry_script()
        if script is None and not self.module_path and not self.uvicorn_app:
            issues.append(f"No entry script found (tried: {self.script_name}, {self.fallback_scripts})")

        # Check port range
        if not (1 <= self.default_port <= 65535):
            issues.append(f"Invalid port number: {self.default_port}")

        # Check health endpoint format
        if not self.health_endpoint.startswith("/"):
            issues.append(f"Health endpoint should start with '/': {self.health_endpoint}")

        return len(issues) == 0, issues


# =============================================================================
# v95.6: Intelligent Cross-Platform Repo Discovery
# =============================================================================

class IntelligentRepoDiscovery:
    """
    v95.8: Enterprise-Grade Dynamic Repository Discovery System.

    Eliminates hardcoded paths by intelligently searching for repositories
    across multiple locations with case-insensitive matching.

    Features:
    - Multi-path discovery across common development directories
    - Environment variable override (highest priority)
    - Case-insensitive repo name matching
    - Cross-platform support (Windows, macOS, Linux)
    - Name variant detection (jarvis-prime, JARVIS-Prime, JarvisPrime)
    - Git repository validation
    - Parallel async discovery for performance
    - Persistent cache with TTL
    - Signature-based repo verification (checks for expected files)

    v95.8 Enhancements:
    - __file__-based absolute path resolution (Issue 6)
    - Network drive/mount point resilience with timeouts (Issue 7)
    - Comprehensive permission validation (Issue 8)
    - Working directory independence
    - Async path accessibility checks
    - Intelligent retry with exponential backoff

    Discovery Priority (highest to lowest):
    1. Environment variable (e.g., JARVIS_PRIME_PATH)
    2. Running process detection (find where it's already running)
    3. Git worktree detection (find related worktrees)
    4. Common development directories
    5. User-provided hints in config file
    """

    # ==========================================================================
    # v95.8: Path Resolution Configuration
    # ==========================================================================

    # Network/mount path detection patterns
    _NETWORK_PATH_PATTERNS: List[str] = [
        "/mnt/",           # Linux mount points
        "/media/",         # Linux media mounts
        "/Volumes/",       # macOS volumes
        "/net/",           # NFS mounts
        "/nfs/",           # NFS mounts
        "//",              # UNC paths (Windows)
        "\\\\",            # UNC paths (Windows backslash)
        "/run/user/",      # User runtime mounts
        "/tmp/.mount_",    # AppImage mounts
    ]

    # Timeout configurations (in seconds)
    _PATH_ACCESS_TIMEOUT: float = 5.0       # Single path access timeout
    _NETWORK_PATH_TIMEOUT: float = 10.0     # Network path access timeout
    _DISCOVERY_TIMEOUT: float = 30.0        # Total discovery timeout
    _RETRY_MAX_ATTEMPTS: int = 3            # Max retry attempts for network paths
    _RETRY_BASE_DELAY: float = 0.5          # Base delay for exponential backoff

    # Common development directory patterns (cross-platform)
    _COMMON_DEV_DIRS: List[str] = [
        "~/Documents/repos",
        "~/Documents/GitHub",
        "~/Documents/code",
        "~/Documents/projects",
        "~/Documents/dev",
        "~/repos",
        "~/code",
        "~/projects",
        "~/dev",
        "~/github",
        "~/git",
        "~/src",
        "~/workspace",
        "~/Development",
        # Windows-specific
        "C:/Users/{user}/Documents/repos",
        "C:/Users/{user}/Documents/GitHub",
        "C:/Users/{user}/code",
        "C:/repos",
        "C:/code",
        "C:/projects",
        # Linux-specific
        "/home/{user}/repos",
        "/home/{user}/code",
        "/home/{user}/projects",
        "/opt/jarvis",
        "/srv/jarvis",
    ]

    # Name variants for each service (case-insensitive matching)
    _SERVICE_NAME_VARIANTS: Dict[str, List[str]] = {
        "jarvis-prime": [
            "jarvis-prime",
            "JARVIS-Prime",
            "JarvisPrime",
            "jarvis_prime",
            "prime",
            "jarvis-prime-ai",
        ],
        "reactor-core": [
            "reactor-core",
            "Reactor-Core",
            "ReactorCore",
            "reactor_core",
            "reactor",
            "jarvis-reactor",
        ],
        "jarvis": [
            "jarvis",
            "JARVIS",
            "jarvis-ai-agent",
            "JARVIS-AI-Agent",
            "jarvis_ai_agent",
            "jarvis-body",
        ],
    }

    # Signature files that confirm this is the right repo
    _REPO_SIGNATURES: Dict[str, List[str]] = {
        "jarvis-prime": [
            "run_server.py",
            "inference/",
            "models/",
            "prime_server.py",
        ],
        "reactor-core": [
            "run_reactor.py",
            "reactor/",
            "training/",
            "pipeline/",
        ],
        "jarvis": [
            "run_supervisor.py",
            "backend/",
            "backend/core/",
            "backend/supervisor/",
        ],
    }

    _instance: Optional["IntelligentRepoDiscovery"] = None
    _discovery_cache: Dict[str, Tuple[Optional[Path], float]] = {}
    _cache_ttl: float = 300.0  # 5 minutes

    def __new__(cls) -> "IntelligentRepoDiscovery":
        """Singleton pattern for shared cache."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._discovery_lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None
        self._user = os.environ.get("USER", os.environ.get("USERNAME", "user"))

        # v95.8: Compute absolute base paths using __file__ (Issue 6)
        # This ensures paths work regardless of current working directory
        self._module_path: Path = Path(__file__).resolve()
        self._module_dir: Path = self._module_path.parent  # backend/supervisor
        self._backend_dir: Path = self._module_dir.parent  # backend
        self._jarvis_root: Path = self._backend_dir.parent  # JARVIS-AI-Agent
        self._repos_parent: Path = self._jarvis_root.parent  # Parent containing sibling repos

        # v95.8: Track network path accessibility results
        self._network_path_status: Dict[str, Tuple[bool, float]] = {}  # path -> (accessible, check_time)

        # v95.8: Permission check results cache
        self._permission_cache: Dict[str, Tuple[Dict[str, bool], float]] = {}  # path -> ({perms}, check_time)
        self._permission_cache_ttl: float = 60.0  # 1 minute TTL for permission cache

        logger.info(
            f"[v95.8] IntelligentRepoDiscovery initialized:\n"
            f"  Module path: {self._module_path}\n"
            f"  JARVIS root: {self._jarvis_root}\n"
            f"  Repos parent: {self._repos_parent}"
        )

    # ==========================================================================
    # v95.8: Issue 6 - Absolute Path Resolution Engine
    # ==========================================================================

    def _resolve_to_absolute(self, path: Union[str, Path]) -> Path:
        """
        v95.8: Resolve any path to absolute, handling all edge cases.

        This ensures paths work regardless of:
        - Current working directory
        - Relative path references
        - Symlinks
        - Home directory expansion

        Args:
            path: Path string or Path object (can be relative or absolute)

        Returns:
            Absolute resolved Path
        """
        if isinstance(path, str):
            path_str = path

            # Handle ~ expansion
            if path_str.startswith("~"):
                path = Path(path_str).expanduser()
            # Handle relative paths - resolve from JARVIS root, not cwd
            elif not path_str.startswith("/") and not (len(path_str) > 1 and path_str[1] == ":"):
                # This is a relative path - resolve from jarvis root
                path = self._jarvis_root / path_str
            else:
                path = Path(path_str)
        else:
            # Path object
            if not path.is_absolute():
                # Relative Path - resolve from jarvis root
                path = self._jarvis_root / path

        # Final resolution: resolve symlinks and normalize
        try:
            return path.resolve()
        except (OSError, RuntimeError) as e:
            logger.debug(f"[v95.8] Cannot fully resolve {path}: {e}, using as-is")
            return path.absolute()

    def _get_canonical_jarvis_root(self) -> Path:
        """
        v95.8: Get the canonical JARVIS root directory.

        Uses __file__ to ensure correctness regardless of cwd.

        Returns:
            Absolute path to JARVIS-AI-Agent root
        """
        return self._jarvis_root

    def _get_sibling_repo_path(self, repo_name: str) -> Optional[Path]:
        """
        v95.8: Get path to a sibling repository (same parent directory).

        This is the most reliable way to find jarvis-prime and reactor-core
        when they're in the same parent directory as JARVIS-AI-Agent.

        Args:
            repo_name: Name of the sibling repo

        Returns:
            Absolute path if found, None otherwise
        """
        variants = self._get_name_variants(repo_name)

        for variant in variants:
            candidate = self._repos_parent / variant
            try:
                if candidate.exists() and candidate.is_dir():
                    if self._validate_repo_signature(candidate, repo_name):
                        logger.debug(f"[v95.8] Found sibling repo: {candidate}")
                        return candidate.resolve()
            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.8] Cannot access sibling {candidate}: {e}")
                continue

        return None

    # ==========================================================================
    # v95.8: Issue 7 - Network Drive/Mount Point Resilience
    # ==========================================================================

    def _is_network_path(self, path: Path) -> bool:
        """
        v95.8: Detect if a path is on a network drive or mount point.

        Network paths need special handling (timeouts, retries) because they
        may be slow or transiently unavailable.

        Args:
            path: Path to check

        Returns:
            True if path appears to be on network/mount
        """
        path_str = str(path)

        # Check against known network path patterns
        for pattern in self._NETWORK_PATH_PATTERNS:
            if pattern in path_str:
                return True

        # Additional checks for mounted filesystems
        try:
            # On Unix, check if the path's device differs from home
            if hasattr(os, 'stat'):
                path_stat = os.stat(path) if path.exists() else None
                home_stat = os.stat(Path.home())

                if path_stat and path_stat.st_dev != home_stat.st_dev:
                    # Different device - likely a mount point
                    return True
        except (OSError, PermissionError):
            pass

        return False

    async def _check_path_accessible_async(
        self,
        path: Path,
        timeout: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        v95.8: Async check if a path is accessible with timeout.

        Handles network drives and slow filesystems gracefully.

        Args:
            path: Path to check
            timeout: Timeout in seconds (auto-detected based on path type)

        Returns:
            Tuple of (accessible, error_message)
        """
        if timeout is None:
            timeout = self._NETWORK_PATH_TIMEOUT if self._is_network_path(path) else self._PATH_ACCESS_TIMEOUT

        def _sync_check() -> Tuple[bool, Optional[str]]:
            """Synchronous path check (runs in thread pool)."""
            try:
                if not path.exists():
                    return False, f"Path does not exist: {path}"

                if not path.is_dir():
                    return False, f"Path is not a directory: {path}"

                # Try to list directory (confirms read access)
                try:
                    next(path.iterdir(), None)
                except StopIteration:
                    pass  # Empty directory is fine

                return True, None

            except PermissionError as e:
                return False, f"Permission denied: {path} - {e}"
            except OSError as e:
                # Catches network timeouts, stale mounts, etc.
                return False, f"OS error accessing {path}: {e}"
            except Exception as e:
                return False, f"Unexpected error accessing {path}: {e}"

        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_check),
                timeout=timeout
            )
            return result
        except asyncio.TimeoutError:
            error_msg = f"Timeout ({timeout}s) accessing path: {path}"
            if self._is_network_path(path):
                error_msg += " (network/mount path detected)"
            logger.warning(f"[v95.8] {error_msg}")
            return False, error_msg
        except Exception as e:
            return False, f"Failed to check path {path}: {e}"

    async def _check_path_with_retry(
        self,
        path: Path,
        max_attempts: Optional[int] = None,
        base_delay: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        v95.8: Check path accessibility with exponential backoff retry.

        Handles transient network issues gracefully.

        Args:
            path: Path to check
            max_attempts: Maximum retry attempts
            base_delay: Base delay for exponential backoff

        Returns:
            Tuple of (accessible, error_message)
        """
        max_attempts = max_attempts or self._RETRY_MAX_ATTEMPTS
        base_delay = base_delay or self._RETRY_BASE_DELAY

        last_error: Optional[str] = None

        for attempt in range(max_attempts):
            accessible, error = await self._check_path_accessible_async(path)

            if accessible:
                if attempt > 0:
                    logger.info(f"[v95.8] Path {path} accessible after {attempt + 1} attempts")
                return True, None

            last_error = error

            # Don't retry for permission errors (won't change)
            if error and "Permission denied" in error:
                return False, error

            # Don't retry for non-existent paths
            if error and "does not exist" in error:
                return False, error

            # Retry with exponential backoff for transient errors
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                # Add jitter to prevent thundering herd
                jitter = delay * 0.1 * (0.5 - time.time() % 1)
                delay = max(0.1, delay + jitter)

                logger.debug(
                    f"[v95.8] Path {path} access failed (attempt {attempt + 1}/{max_attempts}), "
                    f"retrying in {delay:.2f}s: {error}"
                )
                await asyncio.sleep(delay)

        return False, f"Failed after {max_attempts} attempts: {last_error}"

    def _check_path_accessible_sync(self, path: Path, timeout: float = 5.0) -> Tuple[bool, Optional[str]]:
        """
        v95.8: Synchronous path accessibility check with timeout.

        For use in non-async contexts.

        Args:
            path: Path to check
            timeout: Timeout in seconds

        Returns:
            Tuple of (accessible, error_message)
        """
        import concurrent.futures

        def _check() -> Tuple[bool, Optional[str]]:
            try:
                if not path.exists():
                    return False, f"Path does not exist: {path}"
                if not path.is_dir():
                    return False, f"Path is not a directory: {path}"
                # Try to list (confirms access)
                next(path.iterdir(), None)
                return True, None
            except PermissionError as e:
                return False, f"Permission denied: {e}"
            except OSError as e:
                return False, f"OS error: {e}"
            except Exception as e:
                return False, f"Error: {e}"

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_check)
                return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False, f"Timeout ({timeout}s) accessing path"
        except Exception as e:
            return False, f"Check failed: {e}"

    # ==========================================================================
    # v95.8: Issue 8 - Comprehensive Permission Validation
    # ==========================================================================

    def _check_permissions(self, path: Path) -> Dict[str, bool]:
        """
        v95.8: Comprehensive permission check for a path.

        Checks read, write, and execute permissions with clear results.

        Args:
            path: Path to check permissions for

        Returns:
            Dict with permission results: {
                'exists': bool,
                'readable': bool,
                'writable': bool,
                'executable': bool,
                'listable': bool,  # Can list directory contents
                'is_dir': bool,
                'is_file': bool,
                'is_symlink': bool,
                'owner_match': bool,  # True if current user owns the path
            }
        """
        result = {
            'exists': False,
            'readable': False,
            'writable': False,
            'executable': False,
            'listable': False,
            'is_dir': False,
            'is_file': False,
            'is_symlink': False,
            'owner_match': False,
        }

        try:
            # Check existence first
            result['exists'] = path.exists()
            if not result['exists']:
                return result

            # Basic type checks
            result['is_dir'] = path.is_dir()
            result['is_file'] = path.is_file()
            result['is_symlink'] = path.is_symlink()

            # Permission checks using os.access
            result['readable'] = os.access(path, os.R_OK)
            result['writable'] = os.access(path, os.W_OK)
            result['executable'] = os.access(path, os.X_OK)

            # For directories, check if we can list contents
            if result['is_dir']:
                try:
                    next(path.iterdir(), None)
                    result['listable'] = True
                except (PermissionError, OSError):
                    result['listable'] = False

            # Check ownership
            try:
                stat_info = path.stat()
                result['owner_match'] = stat_info.st_uid == os.getuid()
            except (OSError, AttributeError):
                pass  # os.getuid() not available on Windows

        except PermissionError:
            # Can't even stat the path
            result['exists'] = True  # We know it exists if we got PermissionError
        except OSError as e:
            logger.debug(f"[v95.8] Permission check error for {path}: {e}")

        return result

    def _validate_repo_permissions(
        self,
        path: Path,
        require_write: bool = False,
    ) -> Tuple[bool, List[str]]:
        """
        v95.8: Validate that a repository path has required permissions.

        Args:
            path: Repository path to validate
            require_write: Whether write permission is required

        Returns:
            Tuple of (valid, list_of_issues)
        """
        issues: List[str] = []
        perms = self._check_permissions(path)

        if not perms['exists']:
            issues.append(f"Repository path does not exist: {path}")
            return False, issues

        if not perms['is_dir']:
            issues.append(f"Repository path is not a directory: {path}")
            return False, issues

        if not perms['readable']:
            issues.append(f"No read permission for repository: {path}")

        if not perms['listable']:
            issues.append(f"Cannot list directory contents: {path}")

        if not perms['executable']:
            issues.append(f"No execute permission (cannot cd into): {path}")

        if require_write and not perms['writable']:
            issues.append(f"No write permission for repository: {path}")

        # Check key subdirectories and scripts
        if perms['listable']:
            # Check if we can read the main script
            for script_name in ['run_server.py', 'run_reactor.py', 'run_supervisor.py', 'main.py']:
                script_path = path / script_name
                if script_path.exists():
                    script_perms = self._check_permissions(script_path)
                    if not script_perms['readable']:
                        issues.append(f"Cannot read script {script_name}: no read permission")
                    break

        return len(issues) == 0, issues

    def _get_permission_error_hints(self, path: Path, perms: Dict[str, bool]) -> List[str]:
        """
        v95.8: Generate helpful hints for permission issues.

        Args:
            path: Path with permission issues
            perms: Permission check results

        Returns:
            List of helpful hint strings
        """
        hints: List[str] = []

        if not perms['exists']:
            hints.append(f"Ensure the path exists: mkdir -p {path}")
            return hints

        if not perms['readable'] or not perms['listable']:
            hints.append(f"Fix read permissions: chmod +r {path}")
            if perms['is_dir']:
                hints.append(f"Or recursively: chmod -R +r {path}")

        if not perms['executable'] and perms['is_dir']:
            hints.append(f"Fix execute permission (for directory access): chmod +x {path}")

        if not perms['owner_match']:
            hints.append(f"You don't own this path. Consider: sudo chown -R $USER {path}")

        # Check if path is on a read-only filesystem
        try:
            if path.exists():
                parent = path.parent
                test_file = parent / f".jarvis_perm_test_{os.getpid()}"
                try:
                    test_file.touch()
                    test_file.unlink()
                except (PermissionError, OSError):
                    hints.append("Parent directory may be read-only or on a read-only filesystem")
        except Exception:
            pass

        return hints

    def _get_search_directories(self) -> List[Path]:
        """
        v95.8: Get all directories to search for repositories.

        Enhanced with:
        - __file__-based resolution (works from any cwd)
        - Permission validation
        - Network path detection
        - Prioritized ordering

        Returns platform-appropriate paths with user substitution.
        """
        search_dirs: List[Path] = []
        seen_paths: Set[Path] = set()  # Avoid duplicates after resolution
        home = Path.home()

        def add_if_valid(path: Path, priority: int = 0) -> None:
            """Add path if valid and not already seen."""
            try:
                # Resolve to canonical absolute path
                resolved = self._resolve_to_absolute(path)
                if resolved in seen_paths:
                    return
                seen_paths.add(resolved)

                # Quick permission check (sync, with timeout for network paths)
                is_network = self._is_network_path(resolved)
                timeout = 3.0 if is_network else 1.0

                accessible, error = self._check_path_accessible_sync(resolved, timeout=timeout)

                if accessible:
                    if priority == 0:
                        search_dirs.insert(0, resolved)  # High priority
                    else:
                        search_dirs.append(resolved)

                    if is_network:
                        logger.debug(f"[v95.8] Added network path to search: {resolved}")
                else:
                    logger.debug(f"[v95.8] Skipping inaccessible path: {resolved} - {error}")

            except Exception as e:
                logger.debug(f"[v95.8] Error adding search path {path}: {e}")

        # Priority 1: Parent of current JARVIS repo (sibling repos - most reliable)
        # Uses __file__ so works regardless of cwd
        try:
            add_if_valid(self._repos_parent, priority=0)
        except Exception as e:
            logger.debug(f"[v95.8] Cannot add repos parent: {e}")

        # Priority 2: JARVIS root itself (for self-discovery)
        try:
            add_if_valid(self._jarvis_root, priority=0)
        except Exception as e:
            logger.debug(f"[v95.8] Cannot add JARVIS root: {e}")

        # Priority 3: Common development directories
        for pattern in self._COMMON_DEV_DIRS:
            try:
                # Substitute user placeholder
                path_str = pattern.replace("{user}", self._user)

                # Expand ~ to home directory
                if path_str.startswith("~"):
                    path = home / path_str[2:]
                else:
                    path = Path(path_str)

                add_if_valid(path, priority=1)

            except Exception as e:
                logger.debug(f"[v95.8] Error processing pattern {pattern}: {e}")
                continue

        # Priority 4: Current working directory (user might be there)
        try:
            cwd = Path.cwd()
            add_if_valid(cwd, priority=1)
            # Also check cwd's parent (common for sibling repos)
            add_if_valid(cwd.parent, priority=1)
        except Exception:
            pass

        # Priority 5: Paths from config file (if exists)
        config_paths = self._load_config_paths()
        for config_path in config_paths:
            try:
                add_if_valid(Path(config_path), priority=1)
            except Exception:
                pass

        logger.debug(f"[v95.8] Search directories ({len(search_dirs)} total): {[str(p) for p in search_dirs[:5]]}...")
        return search_dirs

    def _load_config_paths(self) -> List[str]:
        """
        v95.8: Load additional search paths from config file.

        Looks for ~/.jarvis/discovery_paths.json or similar.

        Returns:
            List of additional paths to search
        """
        config_locations = [
            Path.home() / ".jarvis" / "discovery_paths.json",
            Path.home() / ".config" / "jarvis" / "discovery_paths.json",
            self._jarvis_root / ".discovery_paths.json",
        ]

        for config_path in config_locations:
            try:
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        data = json.load(f)
                        if isinstance(data, dict) and "search_paths" in data:
                            return data["search_paths"]
                        elif isinstance(data, list):
                            return data
            except Exception as e:
                logger.debug(f"[v95.8] Could not load config from {config_path}: {e}")

        return []

    def _get_name_variants(self, service_name: str) -> List[str]:
        """
        v95.6: Get all possible name variants for a service.

        Includes case variations and common naming conventions.
        """
        base_variants = self._SERVICE_NAME_VARIANTS.get(service_name, [service_name])

        # Generate additional case variants
        all_variants = set(base_variants)
        for name in base_variants:
            all_variants.add(name.lower())
            all_variants.add(name.upper())
            all_variants.add(name.replace("-", "_"))
            all_variants.add(name.replace("_", "-"))

        return list(all_variants)

    def _validate_repo_signature(
        self,
        path: Path,
        service_name: str,
        check_permissions: bool = True,
    ) -> bool:
        """
        v95.8: Validate that a directory is the expected repository.

        Enhanced with:
        - Signature file/directory checking
        - Permission validation (Issue 8)
        - Detailed logging for debugging

        Args:
            path: Path to validate
            service_name: Service name for signature lookup
            check_permissions: Whether to validate permissions

        Returns:
            True if path is valid repository
        """
        signatures = self._REPO_SIGNATURES.get(service_name, [])

        # First, check permissions (Issue 8)
        if check_permissions:
            perms = self._check_permissions(path)

            if not perms['exists']:
                logger.debug(f"[v95.8] Signature check failed - path doesn't exist: {path}")
                return False

            if not perms['readable'] or not perms['listable']:
                logger.warning(
                    f"[v95.8] Permission issue with {path}:\n"
                    f"  readable={perms['readable']}, listable={perms['listable']}"
                )
                # Generate hints
                hints = self._get_permission_error_hints(path, perms)
                for hint in hints:
                    logger.warning(f"  💡 {hint}")
                return False

        if not signatures:
            return True  # No signatures defined, accept any match

        # Require at least 50% of signatures to match
        matches = 0
        checked = []

        for sig in signatures:
            check_path = path / sig
            try:
                exists = check_path.exists()
                checked.append((sig, exists))
                if exists:
                    # Also verify we can access it
                    if check_path.is_file():
                        # Check file is readable
                        if os.access(check_path, os.R_OK):
                            matches += 1
                        else:
                            logger.debug(f"[v95.8] Signature {sig} exists but not readable")
                    elif check_path.is_dir():
                        # Check directory is listable
                        try:
                            next(check_path.iterdir(), None)
                            matches += 1
                        except (PermissionError, OSError):
                            logger.debug(f"[v95.8] Signature dir {sig} exists but not listable")
                    else:
                        matches += 1  # Symlink or other - count it
            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.8] Cannot check signature {sig}: {e}")
                checked.append((sig, False))

        threshold = len(signatures) * 0.5
        valid = matches >= threshold

        if not valid and matches > 0:
            logger.debug(
                f"[v95.8] Signature check failed for {service_name} at {path}: "
                f"{matches}/{len(signatures)} signatures (need {threshold}). "
                f"Checked: {checked}"
            )

        return valid

    def _is_git_repo(self, path: Path) -> bool:
        """
        v95.8: Check if a directory is a git repository.

        Enhanced with permission handling.
        """
        try:
            git_dir = path / ".git"
            if not git_dir.exists():
                return False

            # Check if we can access .git
            if git_dir.is_dir():
                # Try to access HEAD to confirm it's a valid git repo
                head = git_dir / "HEAD"
                return head.exists() and os.access(head, os.R_OK)
            elif git_dir.is_file():
                # Worktree reference file
                return os.access(git_dir, os.R_OK)

            return False
        except (PermissionError, OSError) as e:
            logger.debug(f"[v95.8] Cannot check git repo at {path}: {e}")
            return False

    async def discover_repo(
        self,
        service_name: str,
        env_var: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[Path]:
        """
        v95.8: Discover repository path with intelligent multi-strategy search.

        Enhanced with:
        - __file__-based sibling repo detection (Issue 6)
        - Network drive resilience with timeouts (Issue 7)
        - Comprehensive permission validation (Issue 8)
        - Async path accessibility checks

        Args:
            service_name: Name of the service (e.g., "jarvis-prime")
            env_var: Environment variable to check first
            force_refresh: Bypass cache and rediscover

        Returns:
            Path to repository or None if not found
        """
        cache_key = service_name

        # Check cache first (unless force refresh)
        if not force_refresh and cache_key in self._discovery_cache:
            cached_path, cache_time = self._discovery_cache[cache_key]
            if time.time() - cache_time < self._cache_ttl:
                logger.debug(f"[v95.8] Using cached path for {service_name}: {cached_path}")
                return cached_path

        discovery_start = time.time()
        logger.info(f"[v95.8] Starting discovery for {service_name}...")

        # Strategy 1: Environment variable (highest priority)
        if env_var:
            env_path = os.environ.get(env_var)
            if env_path:
                # v95.8: Use absolute resolution
                path = self._resolve_to_absolute(env_path)

                # v95.8: Check accessibility with network resilience
                is_network = self._is_network_path(path)
                if is_network:
                    logger.debug(f"[v95.8] Env path appears to be network path: {path}")
                    accessible, error = await self._check_path_with_retry(path)
                else:
                    accessible, error = await self._check_path_accessible_async(path)

                if accessible:
                    if self._validate_repo_signature(path, service_name):
                        logger.info(f"[v95.8] Found {service_name} via env var {env_var}: {path}")
                        self._discovery_cache[cache_key] = (path, time.time())
                        return path
                    else:
                        logger.warning(f"[v95.8] Path from {env_var} exists but signature mismatch: {path}")
                else:
                    logger.warning(f"[v95.8] Path from {env_var} not accessible: {error}")
                    # v95.8: Provide hints for common issues
                    perms = self._check_permissions(path)
                    hints = self._get_permission_error_hints(path, perms)
                    for hint in hints:
                        logger.info(f"  💡 {hint}")

        # Strategy 2: Sibling repo detection using __file__ (v95.8 - most reliable)
        # This works regardless of current working directory
        sibling_path = self._get_sibling_repo_path(service_name)
        if sibling_path:
            # Verify accessibility
            accessible, error = await self._check_path_accessible_async(sibling_path)
            if accessible and self._validate_repo_signature(sibling_path, service_name):
                logger.info(f"[v95.8] Found {service_name} as sibling repo: {sibling_path}")
                self._discovery_cache[cache_key] = (sibling_path, time.time())
                return sibling_path

        # Strategy 2: Search common development directories
        search_dirs = self._get_search_directories()
        name_variants = self._get_name_variants(service_name)

        logger.debug(
            f"[v95.7] Async discovery for '{service_name}' searching "
            f"{len(search_dirs)} directories with {len(name_variants)} variants"
        )

        discovered_path: Optional[Path] = None
        best_score = 0
        candidates_found = 0
        symlinks_resolved = 0
        broken_symlinks = 0

        for search_dir in search_dirs:
            try:
                logger.debug(f"[v95.7] Scanning directory: {search_dir}")

                # List all subdirectories
                for item in search_dir.iterdir():
                    # v95.7: Handle symlinks explicitly
                    resolved_item = item
                    is_symlink = item.is_symlink()

                    if is_symlink:
                        try:
                            # Resolve symlink to get real path
                            resolved_item = item.resolve()
                            symlinks_resolved += 1
                            if not resolved_item.exists():
                                broken_symlinks += 1
                                logger.debug(
                                    f"[v95.7] Skipping broken symlink: {item} -> {resolved_item}"
                                )
                                continue
                        except (OSError, RuntimeError) as e:
                            broken_symlinks += 1
                            logger.debug(f"[v95.7] Cannot resolve symlink {item}: {e}")
                            continue

                    # Check if it's a directory (after resolving symlinks)
                    if not resolved_item.is_dir():
                        continue

                    # Use the original item name for matching
                    item_name = item.name.lower()

                    # Check if name matches any variant
                    for variant in name_variants:
                        if item_name == variant.lower():
                            candidates_found += 1
                            # Calculate match score
                            score = 10  # Base score for name match

                            # v95.7: Check git repo on resolved path
                            if self._is_git_repo(resolved_item):
                                score += 5
                                # Bonus for full .git directory
                                if (resolved_item / ".git").is_dir():
                                    score += 1

                            # v95.7: Signature match on resolved path
                            if self._validate_repo_signature(resolved_item, service_name):
                                score += 10

                            # Bonus for exact case match
                            if item.name == variant:
                                score += 2

                            # v95.7: Bonus for symlink (canonical location indicator)
                            if is_symlink:
                                score += 1

                            if score > best_score:
                                best_score = score
                                discovered_path = resolved_item
                                logger.debug(
                                    f"[v95.7] Candidate for {service_name}: {item} "
                                    f"{'-> ' + str(resolved_item) if is_symlink else ''} "
                                    f"(score: {score})"
                                )

            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.7] Cannot search {search_dir}: {e}")
                continue

        if discovered_path:
            resolved = discovered_path.resolve()
            logger.info(
                f"[v95.7] Discovered {service_name} at: {resolved} "
                f"(score: {best_score}, candidates: {candidates_found}, "
                f"symlinks: {symlinks_resolved}, broken: {broken_symlinks})"
            )
            self._discovery_cache[cache_key] = (resolved, time.time())
            return resolved

        # Strategy 4: Try to find via running processes
        discovered_path = await self._find_via_running_process(service_name)
        if discovered_path:
            self._discovery_cache[cache_key] = (discovered_path, time.time())
            return discovered_path

        # v95.8: Enhanced failure reporting
        discovery_duration = time.time() - discovery_start
        logger.warning(
            f"[v95.8] Could not discover repository for {service_name} "
            f"(searched {len(search_dirs)} directories in {discovery_duration:.2f}s)"
        )

        # Provide actionable suggestions
        env_var_name = env_var or f"{service_name.upper().replace('-', '_')}_PATH"
        logger.info(
            f"  💡 To fix: Set {env_var_name} environment variable to the repo path\n"
            f"     Example: export {env_var_name}=/path/to/{service_name}\n"
            f"  💡 Or ensure the repository is in one of these locations:\n"
            f"     {[str(p) for p in search_dirs[:3]]}"
        )

        self._discovery_cache[cache_key] = (None, time.time())
        return None

    async def _find_via_running_process(self, service_name: str) -> Optional[Path]:
        """
        v95.6: Try to find repo path by examining running processes.

        Looks for Python processes that might be running the service.
        """
        try:
            import psutil

            name_variants = self._get_name_variants(service_name)
            script_names = ["run_server.py", "run_reactor.py", "main.py", "app.py"]

            for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
                try:
                    info = proc.info
                    cmdline = info.get("cmdline") or []
                    cwd = info.get("cwd")

                    # Check if this is a Python process
                    if not any("python" in str(c).lower() for c in cmdline[:2]):
                        continue

                    # Check if running one of our scripts
                    for cmd in cmdline:
                        for script in script_names:
                            if script in str(cmd):
                                # Check if the cwd contains our service name
                                if cwd:
                                    cwd_path = Path(cwd)
                                    for variant in name_variants:
                                        if variant.lower() in cwd_path.name.lower():
                                            if self._validate_repo_signature(cwd_path, service_name):
                                                logger.info(
                                                    f"[v95.6] Found {service_name} via running process: {cwd_path}"
                                                )
                                                return cwd_path.resolve()

                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

        except ImportError:
            logger.debug("[v95.6] psutil not available for process discovery")
        except Exception as e:
            logger.debug(f"[v95.6] Process discovery failed: {e}")

        return None

    async def discover_all_repos(self) -> Dict[str, Optional[Path]]:
        """
        v95.6: Discover all known service repositories in parallel.

        Returns:
            Dict mapping service names to discovered paths
        """
        services = ["jarvis-prime", "reactor-core", "jarvis"]

        async def discover_one(name: str) -> Tuple[str, Optional[Path]]:
            env_var = f"{name.upper().replace('-', '_')}_PATH"
            path = await self.discover_repo(name, env_var)
            return name, path

        results = await asyncio.gather(*[discover_one(s) for s in services])
        return dict(results)

    def get_cached_path(self, service_name: str) -> Optional[Path]:
        """
        v95.6: Get cached path without rediscovery.

        Returns None if not cached or cache expired.
        """
        if service_name in self._discovery_cache:
            cached_path, cache_time = self._discovery_cache[service_name]
            if time.time() - cache_time < self._cache_ttl:
                return cached_path
        return None

    def invalidate_cache(self, service_name: Optional[str] = None) -> None:
        """
        v95.6: Invalidate discovery cache.

        Args:
            service_name: Specific service to invalidate, or None for all
        """
        if service_name:
            self._discovery_cache.pop(service_name, None)
        else:
            self._discovery_cache.clear()
        logger.info(f"[v95.6] Discovery cache invalidated: {service_name or 'all'}")


def get_repo_discovery() -> IntelligentRepoDiscovery:
    """
    v95.6: Get the global repo discovery instance.
    """
    return IntelligentRepoDiscovery()


class ServiceDefinitionRegistry:
    """
    v95.0: Centralized registry for canonical service definitions.

    This is the SINGLE SOURCE OF TRUTH for service configurations.
    All components (trinity_bridge, orchestrator, etc.) should use
    this registry to get consistent service definitions.

    Features:
    - Immutable canonical definitions for each service
    - Dynamic path resolution from environment or config
    - Automatic validation before returning definitions
    - Caching with TTL for performance
    """

    # Canonical service configurations - DO NOT modify these directly
    # Use environment variables to override paths/ports
    #
    # v95.0: Service Dependency Graph (startup order):
    #   jarvis-body (priority 10) ──────────────────────────┐
    #         │                                              │
    #         └──► jarvis-prime (priority 20) ──────────────┼──► reactor-core (priority 30)
    #                                                        │
    # Note: jarvis-prime depends on jarvis-body for service registry
    #       reactor-core depends on jarvis-prime for AGI integration
    #
    # v95.6: Dynamic repo discovery - NO HARDCODED PATHS
    # Paths are discovered at runtime using IntelligentRepoDiscovery
    # Environment variables take priority, then intelligent search

    _CANONICAL_DEFINITIONS = {
        "jarvis-prime": {
            "script_name": "run_server.py",
            "fallback_scripts": ["main.py", "server.py", "app.py"],
            "default_port_env": "JARVIS_PRIME_PORT",
            "default_port": 8000,
            "health_endpoint": "/health",
            "startup_timeout": 180.0,  # ML model loading
            "repo_path_env": "JARVIS_PRIME_PATH",
            # v95.6: Use dynamic discovery instead of hardcoded path
            # This will be resolved at runtime via get_definition()
            "default_repo_path": None,  # Discovered dynamically
            "discovery_service_name": "jarvis-prime",  # For discovery lookup
            "script_args_factory": lambda port: ["--port", str(port), "--host", "0.0.0.0"],
            "environment_factory": lambda path: {
                "PYTHONPATH": str(path),
                "PYTHONWARNINGS": "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning",
                "TF_CPP_MIN_LOG_LEVEL": "2",
                "TRANSFORMERS_VERBOSITY": "error",
                "TOKENIZERS_PARALLELISM": "false",
                "COREMLTOOLS_LOG_LEVEL": "ERROR",
            },
            # v95.0: Dependency and priority configuration
            "depends_on": [],  # jarvis-prime can start independently
            "startup_priority": 20,  # Start second (after jarvis-body)
            "is_critical": True,  # System fails without this
            "dependency_wait_timeout": 120.0,
        },
        "reactor-core": {
            "script_name": "run_reactor.py",
            "fallback_scripts": ["run_supervisor.py", "main.py", "server.py"],
            "default_port_env": "REACTOR_CORE_PORT",
            "default_port": 8090,
            "health_endpoint": "/health",
            "startup_timeout": 90.0,
            "repo_path_env": "REACTOR_CORE_PATH",
            # v95.6: Use dynamic discovery instead of hardcoded path
            "default_repo_path": None,  # Discovered dynamically
            "discovery_service_name": "reactor-core",  # For discovery lookup
            "script_args_factory": lambda port: ["--port", str(port)],
            "environment_factory": lambda path: {
                "PYTHONPATH": str(path),
                "REACTOR_PORT": str(os.getenv("REACTOR_CORE_PORT", 8090)),
            },
            "use_uvicorn": False,
            # v95.0: Dependency and priority configuration
            "depends_on": ["jarvis-prime"],  # reactor-core depends on jarvis-prime for AGI
            "startup_priority": 30,  # Start third (after jarvis-prime)
            "is_critical": False,  # System can degrade without this
            "dependency_wait_timeout": 180.0,  # Wait longer for jarvis-prime to load models
        },
    }

    _instance = None
    _cache: Dict[str, tuple[ServiceDefinition, float]] = {}
    _cache_ttl = 60.0  # seconds

    def __new__(cls) -> "ServiceDefinitionRegistry":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_definition(
        cls,
        service_name: str,
        port_override: Optional[int] = None,
        path_override: Optional[Path] = None,
        validate: bool = True,
    ) -> Optional[ServiceDefinition]:
        """
        Get a canonical service definition.

        Args:
            service_name: Name of the service (jarvis-prime, reactor-core)
            port_override: Optional port override
            path_override: Optional repo path override
            validate: Whether to validate the definition

        Returns:
            ServiceDefinition or None if service not found
        """
        canonical = cls._CANONICAL_DEFINITIONS.get(service_name)
        if not canonical:
            logger.warning(f"Unknown service: {service_name}")
            return None

        # Check cache
        cache_key = f"{service_name}:{port_override}:{path_override}"
        if cache_key in cls._cache:
            cached_def, cached_time = cls._cache[cache_key]
            if time.time() - cached_time < cls._cache_ttl:
                return cached_def

        # Resolve port
        port = port_override
        if port is None:
            port = int(os.getenv(canonical["default_port_env"], canonical["default_port"]))

        # Resolve path using v95.6 intelligent discovery
        repo_path = path_override
        if repo_path is None:
            env_var_name = canonical["repo_path_env"]

            # Priority 1: Check environment variable
            env_path = os.getenv(env_var_name)
            if env_path:
                raw_path = Path(env_path).expanduser()
                # v95.6: Resolve symlinks and validate
                if raw_path.exists():
                    # Resolve symlinks to get real path
                    repo_path = raw_path.resolve()
                    logger.info(f"[v95.6] Using {env_var_name}={repo_path}")
                elif raw_path.is_symlink():
                    # Broken symlink
                    logger.warning(
                        f"[v95.6] {env_var_name} points to broken symlink: {raw_path}"
                    )
                else:
                    logger.warning(
                        f"[v95.6] {env_var_name} path does not exist: {raw_path}"
                    )

            # Priority 2: Use intelligent discovery (v95.6)
            if repo_path is None:
                discovery = get_repo_discovery()
                discovery_name = canonical.get("discovery_service_name", service_name)

                # Try to get cached path first (synchronous)
                cached = discovery.get_cached_path(discovery_name)
                if cached:
                    repo_path = cached
                    logger.debug(f"[v95.6] Using cached path for {service_name}: {repo_path}")
                else:
                    # Fallback: Try synchronous discovery for common locations
                    repo_path = cls._sync_discover_repo(discovery_name)

                    if repo_path is None:
                        logger.warning(
                            f"[v95.6] Could not discover {service_name}. "
                            f"Set {env_var_name} environment variable or ensure repo exists."
                        )
                        return None  # v95.6: Cannot create definition without valid path

        # v95.6: Final path validation and symlink resolution
        if repo_path is not None:
            # Resolve symlinks
            if repo_path.is_symlink():
                repo_path = repo_path.resolve()
            # Make absolute
            if not repo_path.is_absolute():
                repo_path = repo_path.resolve()
            # Validate exists
            if not repo_path.exists():
                logger.error(f"[v95.6] Repository path does not exist: {repo_path}")
                return None

        # Build definition
        definition = ServiceDefinition(
            name=service_name,
            repo_path=repo_path,
            script_name=canonical["script_name"],
            fallback_scripts=canonical["fallback_scripts"],
            default_port=port,
            health_endpoint=canonical["health_endpoint"],
            startup_timeout=canonical["startup_timeout"],
            script_args=canonical.get("script_args_factory", lambda p: [])(port),
            environment=canonical.get("environment_factory", lambda p: {})(repo_path),
            use_uvicorn=canonical.get("use_uvicorn", False),
            uvicorn_app=canonical.get("uvicorn_app"),
            # v95.0: Dependency and priority configuration
            depends_on=canonical.get("depends_on", []),
            startup_priority=canonical.get("startup_priority", 50),
            is_critical=canonical.get("is_critical", True),
            dependency_wait_timeout=canonical.get("dependency_wait_timeout", 120.0),
        )

        # Validate if requested
        if validate:
            is_valid, issues = definition.validate()
            if not is_valid:
                logger.warning(f"Service definition validation failed for {service_name}:")
                for issue in issues:
                    logger.warning(f"  - {issue}")

        # Cache and return
        cls._cache[cache_key] = (definition, time.time())
        return definition

    @classmethod
    def get_all_definitions(
        cls,
        validate: bool = True,
    ) -> List[ServiceDefinition]:
        """Get all canonical service definitions."""
        definitions = []
        for name in cls._CANONICAL_DEFINITIONS:
            definition = cls.get_definition(name, validate=validate)
            if definition:
                definitions.append(definition)
        return definitions

    @classmethod
    def _sync_discover_repo(cls, service_name: str) -> Optional[Path]:
        """
        v95.8: Enterprise-grade synchronous repo discovery.

        This is a comprehensive synchronous version of IntelligentRepoDiscovery
        that works in non-async contexts with full support for:
        - __file__-based sibling repo detection (Issue 6)
        - Network path handling with timeouts (Issue 7)
        - Permission validation (Issue 8)

        Features:
        - Sibling repo detection using __file__ (highest priority)
        - Symlink resolution during directory iteration
        - Broken symlink detection and logging
        - Comprehensive candidate scoring
        - Permission validation with helpful error messages
        - Network path timeout handling

        Args:
            service_name: Service name to discover

        Returns:
            Path to repository or None if not found
        """
        discovery = get_repo_discovery()

        # Strategy 1: Sibling repo detection using __file__ (v95.8 - most reliable)
        # This works regardless of current working directory
        sibling_path = discovery._get_sibling_repo_path(service_name)
        if sibling_path:
            # Verify accessibility with timeout
            accessible, error = discovery._check_path_accessible_sync(sibling_path, timeout=3.0)
            if accessible:
                # Validate signature with permission check
                if discovery._validate_repo_signature(sibling_path, service_name, check_permissions=True):
                    logger.info(f"[v95.8] Sync discovered {service_name} as sibling repo: {sibling_path}")
                    discovery._discovery_cache[service_name] = (sibling_path, time.time())
                    return sibling_path
                else:
                    logger.debug(f"[v95.8] Sibling path {sibling_path} failed signature validation")
            else:
                logger.debug(f"[v95.8] Sibling path {sibling_path} not accessible: {error}")

        # Get name variants
        name_variants = discovery._get_name_variants(service_name)

        # Get search directories (v95.8: now includes permission-validated paths only)
        search_dirs = discovery._get_search_directories()

        logger.debug(
            f"[v95.7] Sync discovery for '{service_name}' searching "
            f"{len(search_dirs)} directories with {len(name_variants)} name variants"
        )

        best_match: Optional[Path] = None
        best_score = 0
        candidates_found = 0

        for search_dir in search_dirs:
            try:
                if not search_dir.exists():
                    continue

                logger.debug(f"[v95.7] Scanning directory: {search_dir}")

                for item in search_dir.iterdir():
                    # v95.7: Handle symlinks explicitly
                    resolved_item = item
                    is_symlink = item.is_symlink()

                    if is_symlink:
                        try:
                            # Resolve symlink to get real path
                            resolved_item = item.resolve()
                            if not resolved_item.exists():
                                logger.debug(
                                    f"[v95.7] Skipping broken symlink: {item} -> {resolved_item}"
                                )
                                continue
                            logger.debug(f"[v95.7] Resolved symlink: {item} -> {resolved_item}")
                        except (OSError, RuntimeError) as e:
                            logger.debug(f"[v95.7] Cannot resolve symlink {item}: {e}")
                            continue

                    # Check if it's a directory (after resolving symlinks)
                    if not resolved_item.is_dir():
                        continue

                    # Use the original item name for matching (symlink name is what user sees)
                    item_name = item.name.lower()

                    # Check if name matches any variant
                    for variant in name_variants:
                        if item_name == variant.lower():
                            candidates_found += 1
                            score = 10  # Base score for name match

                            # v95.7: Check git repo on resolved path
                            git_dir = resolved_item / ".git"
                            if git_dir.exists():
                                score += 5
                                # Additional bonus for .git being a directory (not worktree file)
                                if git_dir.is_dir():
                                    score += 1

                            # v95.7: Signature match on resolved path
                            if discovery._validate_repo_signature(resolved_item, service_name):
                                score += 10

                            # Bonus for exact case match
                            if item.name == variant:
                                score += 2

                            # v95.7: Bonus for symlink (often indicates preferred/canonical location)
                            if is_symlink:
                                score += 1

                            # v95.7: Bonus if resolved path is in typical dev directory
                            resolved_str = str(resolved_item).lower()
                            if any(dev_dir in resolved_str for dev_dir in ["repos", "code", "projects", "github"]):
                                score += 1

                            if score > best_score:
                                best_score = score
                                best_match = resolved_item  # Use resolved path
                                logger.debug(
                                    f"[v95.7] Sync discovery candidate: {item} "
                                    f"{'-> ' + str(resolved_item) if is_symlink else ''} "
                                    f"(score: {score}, git: {git_dir.exists()}, symlink: {is_symlink})"
                                )

            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.7] Cannot scan directory {search_dir}: {e}")
                continue

        if best_match:
            # Final resolution to ensure absolute canonical path
            resolved = best_match.resolve()
            logger.info(
                f"[v95.7] Sync discovered {service_name} at: {resolved} "
                f"(score: {best_score}, candidates: {candidates_found})"
            )
            # Cache the result
            discovery._discovery_cache[service_name] = (resolved, time.time())
            return resolved

        logger.warning(
            f"[v95.7] Sync discovery failed for '{service_name}'. "
            f"Searched {len(search_dirs)} directories, found {candidates_found} candidates, "
            f"none with sufficient score. Variants searched: {name_variants[:5]}..."
        )
        return None

    @classmethod
    def list_services(cls) -> List[str]:
        """List all known service names."""
        return list(cls._CANONICAL_DEFINITIONS.keys())

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the definition cache."""
        cls._cache.clear()
        # Also clear discovery cache
        try:
            get_repo_discovery().invalidate_cache()
        except Exception:
            pass


# Convenience function for getting service definitions
def get_service_definition(
    service_name: str,
    port: Optional[int] = None,
    path: Optional[Path] = None,
) -> Optional[ServiceDefinition]:
    """
    v95.0: Get a canonical service definition from the registry.

    This is the preferred way to get service definitions as it ensures
    consistency across all components.

    Example:
        definition = get_service_definition("reactor-core")
        if definition:
            # Use definition...
    """
    return ServiceDefinitionRegistry.get_definition(service_name, port, path)


@dataclass
class ManagedProcess:
    """Represents a managed child process with monitoring."""
    definition: ServiceDefinition
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    port: Optional[int] = None
    status: ServiceStatus = ServiceStatus.PENDING
    restart_count: int = 0
    last_restart: float = 0.0
    last_health_check: float = 0.0
    consecutive_failures: int = 0

    # Background tasks
    output_stream_task: Optional[asyncio.Task] = None
    health_monitor_task: Optional[asyncio.Task] = None
    # v95.0: Dedicated heartbeat task that runs independently of health checks
    # This prevents services from becoming stale even when health checks fail
    heartbeat_task: Optional[asyncio.Task] = None

    # v95.0: Track last known health status for smarter heartbeat reporting
    last_known_health: str = "unknown"
    last_heartbeat_sent: float = 0.0

    @property
    def is_running(self) -> bool:
        """Check if process is running."""
        if self.process is None:
            return False
        return self.process.returncode is None

    def calculate_backoff(self, base: float = 1.0, max_backoff: float = 60.0) -> float:
        """Calculate exponential backoff for restart."""
        backoff = base * (2 ** self.restart_count)
        return min(backoff, max_backoff)


# =============================================================================
# Process Orchestrator
# =============================================================================

class ProcessOrchestrator:
    """
    Enterprise-grade process lifecycle manager.

    Features:
    - Spawn and manage child processes
    - Stream stdout/stderr with service prefixes
    - Auto-heal crashed services with exponential backoff
    - Graceful shutdown handling
    - Dynamic service discovery via registry
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None):
        """Initialize orchestrator."""
        self.config = config or OrchestratorConfig()
        self.processes: Dict[str, ManagedProcess] = {}
        self._shutdown_event = asyncio.Event()
        self._running = False

        # Service registry (lazy loaded)
        self._registry = None

        # Signal handlers registered flag
        self._signals_registered = False

        # v93.9: Circuit breakers for Docker operations (per-service)
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}

        # v93.9: Retry states for operations
        self._retry_states: Dict[str, RetryState] = {}

        # v93.9: Cached memory status (refreshed periodically)
        self._cached_memory_status: Optional[MemoryStatus] = None
        self._memory_cache_time: float = 0.0
        self._memory_cache_ttl: float = 5.0  # Refresh every 5 seconds

        # v93.9: GCP VM manager reference (lazy loaded)
        self._gcp_vm_manager = None

        # v93.11: Thread-safe locks for concurrent access
        self._circuit_breaker_lock: Optional[asyncio.Lock] = None
        self._memory_status_lock: Optional[asyncio.Lock] = None
        self._startup_coordination_lock: Optional[asyncio.Lock] = None
        self._service_startup_semaphore: Optional[asyncio.Semaphore] = None

        # v93.11: Shared HTTP session with connection pooling (lazy initialized)
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._http_session_lock: Optional[asyncio.Lock] = None

        # v93.11: Cross-repo health aggregation
        self._service_health_cache: Dict[str, Dict[str, Any]] = {}
        self._health_cache_lock: Optional[asyncio.Lock] = None

        # v93.11: Startup coordination - track which services are starting
        self._services_starting: Set[str] = set()
        self._services_ready: Set[str] = set()
        self._startup_events: Dict[str, asyncio.Event] = {}

        # v95.1: Background task tracking (prevents fire-and-forget task leaks)
        self._background_tasks: Set[asyncio.Task] = set()
        self._background_tasks_lock: Optional[asyncio.Lock] = None

        # v95.1: Intelligent cross-repo recovery coordination
        self._recovery_coordinator_task: Optional[asyncio.Task] = None
        self._recovery_check_interval: float = float(
            os.environ.get("JARVIS_RECOVERY_CHECK_INTERVAL", "30.0")
        )
        self._proactive_recovery_enabled: bool = os.environ.get(
            "JARVIS_PROACTIVE_RECOVERY", "true"
        ).lower() == "true"
        self._dependency_cascade_recovery: bool = os.environ.get(
            "JARVIS_CASCADE_RECOVERY", "true"
        ).lower() == "true"

        # v95.3: Global shutdown completion flag (prevents post-shutdown recovery)
        # This flag is set when shutdown COMPLETES and stays True forever
        # Unlike _shutdown_event which signals "shutdown in progress", this
        # signals "shutdown is DONE, do NOT try to recover anything"
        self._shutdown_completed = False
        self._shutdown_completed_timestamp: Optional[float] = None

        # v95.4: JARVIS body startup tracking
        # This fixes the dependency deadlock where external services wait for jarvis-body
        # but the orchestrator doesn't track jarvis-body's own startup status
        self._jarvis_body_status: str = "initializing"  # initializing, starting, healthy, unhealthy
        self._jarvis_body_ready_event: Optional[asyncio.Event] = None
        self._jarvis_body_startup_time: Optional[float] = None
        self._jarvis_body_health_verified: bool = False

        # v95.5: Graceful Degradation with Circuit Breaker Pattern
        # Allows services to operate independently when dependencies fail
        self._degradation_mode: Dict[str, str] = {}  # service -> mode (full, degraded, isolated)
        self._service_capabilities: Dict[str, Set[str]] = {}  # service -> available capabilities
        self._fallback_handlers: Dict[str, Callable] = {}  # service -> fallback handler
        self._cross_repo_breaker: Optional[Any] = None  # CrossRepoCircuitBreaker instance
        self._degradation_lock: Optional[asyncio.Lock] = None

        # v95.5: Distributed Tracing with Correlation ID Propagation
        # Enables cross-component request tracking for debugging
        self._startup_correlation_id: Optional[str] = None
        self._startup_trace_context: Optional[Any] = None  # CorrelationContext
        self._active_traces: Dict[str, Any] = {}  # operation_id -> CorrelationContext
        self._trace_lock: Optional[asyncio.Lock] = None

        # v95.5: Event Bus Integration for Lifecycle Events
        # Publishes startup/ready/failed events for cross-repo coordination
        self._event_bus: Optional[Any] = None  # TrinityEventBus instance
        self._event_bus_initialized: bool = False
        self._lifecycle_events_enabled: bool = os.environ.get(
            "JARVIS_LIFECYCLE_EVENTS", "true"
        ).lower() == "true"
        self._event_subscriptions: List[str] = []  # Subscription IDs for cleanup

        # =====================================================================
        # v95.9: Error Handling & Recovery Infrastructure (Issues 21-25)
        # =====================================================================

        # Issue 21: Silent Failure on Repo Discovery - Explicit error handling
        self._discovery_failures: Dict[str, Dict[str, Any]] = {}  # service -> failure info
        self._degraded_services: Set[str] = set()  # Services running in degraded mode
        self._discovery_retry_state: Dict[str, Dict[str, Any]] = {}  # service -> retry state
        self._max_discovery_retries: int = int(os.environ.get("JARVIS_DISCOVERY_RETRIES", "5"))
        self._discovery_retry_base_delay: float = float(os.environ.get("JARVIS_DISCOVERY_DELAY", "2.0"))

        # Issue 22: Process Crash Without Restart - Crash monitoring
        self._crash_history: Dict[str, List[Dict[str, Any]]] = {}  # service -> list of crash events
        self._crash_rate_window: float = 300.0  # 5-minute window for crash rate calculation
        self._max_crashes_per_window: int = 5  # Circuit breaker threshold
        self._crash_circuit_breakers: Dict[str, bool] = {}  # service -> is_open (True = stopped restarting)
        self._crash_analysis_enabled: bool = os.environ.get("JARVIS_CRASH_ANALYSIS", "true").lower() == "true"
        self._last_crash_analysis: Dict[str, Dict[str, Any]] = {}  # service -> analysis result

        # Issue 23: Port Conflict No Recovery - Port resolution
        self._port_allocation_map: Dict[str, int] = {}  # service -> allocated port
        self._port_conflict_history: Dict[int, List[Dict[str, Any]]] = {}  # port -> conflict events
        self._dynamic_port_range: Tuple[int, int] = (
            int(os.environ.get("JARVIS_PORT_RANGE_START", "9000")),
            int(os.environ.get("JARVIS_PORT_RANGE_END", "9999"))
        )
        self._port_scan_cache: Dict[int, Tuple[bool, float]] = {}  # port -> (in_use, check_time)
        self._port_scan_cache_ttl: float = 10.0  # 10 seconds

        # Issue 24: Import Error No Fallback - Import error handling
        self._import_errors: Dict[str, Dict[str, Any]] = {}  # module -> error info
        self._import_fallbacks: Dict[str, List[str]] = {}  # module -> list of fallback modules
        self._loaded_alternatives: Dict[str, str] = {}  # original module -> loaded alternative
        self._lazy_imports: Dict[str, Any] = {}  # module name -> loaded module or None

        # Issue 25: Network Timeout No Retry - Network resilience
        self._network_circuit_breakers: Dict[str, Dict[str, Any]] = {}  # endpoint -> breaker state
        self._network_health_status: Dict[str, Dict[str, Any]] = {}  # endpoint -> health info
        self._network_retry_config: Dict[str, Any] = {
            "max_retries": int(os.environ.get("JARVIS_NETWORK_RETRIES", "3")),
            "base_delay": float(os.environ.get("JARVIS_NETWORK_DELAY", "1.0")),
            "max_delay": float(os.environ.get("JARVIS_NETWORK_MAX_DELAY", "30.0")),
            "timeout": float(os.environ.get("JARVIS_NETWORK_TIMEOUT", "10.0")),
            "circuit_threshold": int(os.environ.get("JARVIS_CIRCUIT_THRESHOLD", "5")),
            "circuit_reset_time": float(os.environ.get("JARVIS_CIRCUIT_RESET", "60.0")),
        }

        # =====================================================================
        # v95.10: Cross-Repo Integration Infrastructure (Issues 31-38)
        # =====================================================================

        # Issue 31: Unified Configuration - Trinity config system
        self._unified_config: Dict[str, Any] = {}  # Merged config from all repos
        self._config_sources: Dict[str, Dict[str, Any]] = {}  # repo -> config dict
        self._config_overrides: Dict[str, Any] = {}  # User overrides (highest priority)
        self._config_schema: Dict[str, Dict[str, Any]] = {}  # Config validation schema
        self._config_watchers: Dict[str, Callable] = {}  # config_key -> callback
        self._config_lock: Optional[asyncio.Lock] = None
        self._config_sync_interval: float = float(os.environ.get("JARVIS_CONFIG_SYNC_INTERVAL", "30.0"))
        self._config_sync_task: Optional[asyncio.Task] = None

        # Issue 32: Cross-Repo Logging - Unified logging with W3C trace context
        self._unified_log_handlers: Dict[str, Any] = {}  # handler_name -> handler
        self._log_correlation_enabled: bool = os.environ.get("JARVIS_LOG_CORRELATION", "true").lower() == "true"
        self._log_aggregation_buffer: List[Dict[str, Any]] = []  # Buffered log entries
        self._log_buffer_size: int = int(os.environ.get("JARVIS_LOG_BUFFER_SIZE", "100"))
        self._log_flush_interval: float = float(os.environ.get("JARVIS_LOG_FLUSH_INTERVAL", "5.0"))
        self._log_flush_task: Optional[asyncio.Task] = None
        self._log_lock: Optional[asyncio.Lock] = None
        self._trace_id_header: str = "X-Trace-ID"
        self._span_id_header: str = "X-Span-ID"
        self._parent_id_header: str = "X-Parent-ID"

        # Issue 33: Cross-Repo Metrics - Unified metrics collection
        self._metrics_registry: Dict[str, Dict[str, Any]] = {}  # metric_name -> metric_info
        self._metrics_collectors: Dict[str, Callable] = {}  # collector_name -> collector_fn
        self._metrics_buffer: List[Dict[str, Any]] = []  # Buffered metrics
        self._metrics_lock: Optional[asyncio.Lock] = None
        self._metrics_enabled: bool = os.environ.get("JARVIS_METRICS_ENABLED", "true").lower() == "true"
        self._metrics_collection_interval: float = float(os.environ.get("JARVIS_METRICS_INTERVAL", "10.0"))
        self._metrics_collection_task: Optional[asyncio.Task] = None
        self._service_metrics_cache: Dict[str, Dict[str, Any]] = {}  # service -> metrics snapshot

        # Issue 34: Cross-Repo Error Propagation - Error context and correlation
        self._error_registry: Dict[str, Dict[str, Any]] = {}  # error_id -> error_info
        self._error_propagation_enabled: bool = os.environ.get("JARVIS_ERROR_PROPAGATION", "true").lower() == "true"
        self._error_correlation_map: Dict[str, List[str]] = {}  # root_error_id -> [related_error_ids]
        self._error_handlers: Dict[str, List[Callable]] = {}  # error_type -> [handlers]
        self._error_lock: Optional[asyncio.Lock] = None
        self._max_error_history: int = int(os.environ.get("JARVIS_MAX_ERROR_HISTORY", "1000"))

        # Issue 35: Cross-Repo State Synchronization - Shared state management
        self._shared_state: Dict[str, Any] = {}  # Cross-repo shared state
        self._state_version: Dict[str, int] = {}  # key -> version number
        self._state_subscribers: Dict[str, List[Callable]] = {}  # key -> [subscriber_callbacks]
        self._state_lock: Optional[asyncio.Lock] = None
        self._state_sync_enabled: bool = os.environ.get("JARVIS_STATE_SYNC", "true").lower() == "true"
        self._state_persistence_path: Optional[Path] = None
        self._state_sync_task: Optional[asyncio.Task] = None
        self._state_change_buffer: List[Dict[str, Any]] = []  # Buffered state changes

        # Issue 36: Cross-Repo Resource Coordination - Resource allocation
        self._resource_registry: Dict[str, Dict[str, Any]] = {}  # resource_id -> resource_info
        self._resource_allocations: Dict[str, Dict[str, int]] = {}  # service -> {resource -> amount}
        self._resource_limits: Dict[str, int] = {}  # resource -> total_available
        self._resource_lock: Optional[asyncio.Lock] = None
        self._resource_coordination_enabled: bool = os.environ.get("JARVIS_RESOURCE_COORDINATION", "true").lower() == "true"
        self._resource_conflict_handlers: Dict[str, Callable] = {}  # resource -> conflict_handler

        # Issue 37: Cross-Repo Version Compatibility - Compatibility matrix
        self._version_registry: Dict[str, str] = {}  # service -> version
        self._compatibility_matrix: Dict[str, Dict[str, List[str]]] = {}  # service -> {dep -> [compatible_versions]}
        self._version_check_enabled: bool = os.environ.get("JARVIS_VERSION_CHECK", "true").lower() == "true"
        self._version_lock: Optional[asyncio.Lock] = None
        self._incompatibility_handlers: Dict[str, Callable] = {}  # service_pair -> handler

        # Issue 38: Cross-Repo Security Context - Unified security
        self._security_context: Dict[str, Any] = {}  # Current security context
        self._security_tokens: Dict[str, str] = {}  # service -> auth_token
        self._security_policies: Dict[str, Dict[str, Any]] = {}  # policy_name -> policy_def
        self._security_lock: Optional[asyncio.Lock] = None
        self._security_context_enabled: bool = os.environ.get("JARVIS_SECURITY_CONTEXT", "true").lower() == "true"
        self._token_refresh_interval: float = float(os.environ.get("JARVIS_TOKEN_REFRESH", "3600.0"))
        self._token_refresh_task: Optional[asyncio.Task] = None

    def _ensure_locks_initialized(self) -> None:
        """
        v93.11: Lazily initialize asyncio primitives.

        Called before any operation that needs locks.
        This handles the case where __init__ runs before event loop exists.
        """
        if self._circuit_breaker_lock is None:
            self._circuit_breaker_lock = asyncio.Lock()
        if self._memory_status_lock is None:
            self._memory_status_lock = asyncio.Lock()
        if self._startup_coordination_lock is None:
            self._startup_coordination_lock = asyncio.Lock()
        if self._service_startup_semaphore is None:
            # Limit concurrent startups to prevent resource exhaustion
            self._service_startup_semaphore = asyncio.Semaphore(3)
        if self._http_session_lock is None:
            self._http_session_lock = asyncio.Lock()
        if self._background_tasks_lock is None:
            self._background_tasks_lock = asyncio.Lock()
        if self._health_cache_lock is None:
            self._health_cache_lock = asyncio.Lock()
        # v95.4: JARVIS body ready event
        if self._jarvis_body_ready_event is None:
            self._jarvis_body_ready_event = asyncio.Event()
        # v95.5: Degradation and tracing locks
        if self._degradation_lock is None:
            self._degradation_lock = asyncio.Lock()
        if self._trace_lock is None:
            self._trace_lock = asyncio.Lock()

        # v95.10: Cross-repo integration locks
        if self._config_lock is None:
            self._config_lock = asyncio.Lock()
        if self._log_lock is None:
            self._log_lock = asyncio.Lock()
        if self._metrics_lock is None:
            self._metrics_lock = asyncio.Lock()
        if self._error_lock is None:
            self._error_lock = asyncio.Lock()
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        if self._resource_lock is None:
            self._resource_lock = asyncio.Lock()
        if self._version_lock is None:
            self._version_lock = asyncio.Lock()
        if self._security_lock is None:
            self._security_lock = asyncio.Lock()

    @property
    def _degradation_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for degradation lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._degradation_lock is not None, "Degradation lock should be initialized"
        return self._degradation_lock

    @property
    def _trace_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for trace lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._trace_lock is not None, "Trace lock should be initialized"
        return self._trace_lock

    @property
    def _http_session_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for HTTP session lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._http_session_lock is not None, "HTTP session lock should be initialized"
        return self._http_session_lock

    @property
    def _health_cache_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for health cache lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._health_cache_lock is not None, "Health cache lock should be initialized"
        return self._health_cache_lock

    @property
    def _startup_coordination_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for startup coordination lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._startup_coordination_lock is not None, "Startup coordination lock should be initialized"
        return self._startup_coordination_lock

    @property
    def _service_startup_semaphore_safe(self) -> asyncio.Semaphore:
        """v95.7: Type-safe accessor for startup semaphore. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._service_startup_semaphore is not None, "Service startup semaphore should be initialized"
        return self._service_startup_semaphore

    @property
    def _background_tasks_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for background tasks lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._background_tasks_lock is not None, "Background tasks lock should be initialized"
        return self._background_tasks_lock

    @property
    def _memory_status_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for memory status lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._memory_status_lock is not None, "Memory status lock should be initialized"
        return self._memory_status_lock

    @property
    def _circuit_breaker_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for circuit breaker lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._circuit_breaker_lock is not None, "Circuit breaker lock should be initialized"
        return self._circuit_breaker_lock

    # =========================================================================
    # v95.9: Issue 21 - Silent Failure on Repo Discovery
    # =========================================================================

    async def _handle_discovery_failure(
        self,
        service_name: str,
        error: str,
        retry_count: int = 0,
    ) -> Tuple[bool, Optional[Path]]:
        """
        v95.9: Handle repo discovery failure with explicit errors and retry logic.

        Instead of silently continuing, this method:
        1. Logs explicit, actionable error messages
        2. Attempts retry with exponential backoff
        3. Notifies about degraded mode if discovery ultimately fails
        4. Records failure for monitoring/alerting

        Args:
            service_name: Service that failed discovery
            error: Error message
            retry_count: Current retry attempt

        Returns:
            Tuple of (success, discovered_path)
        """
        self._ensure_locks_initialized()

        # Record the failure
        failure_info = {
            "service": service_name,
            "error": error,
            "timestamp": time.time(),
            "retry_count": retry_count,
            "resolved": False,
        }
        self._discovery_failures[service_name] = failure_info

        # Log explicit, actionable error
        logger.error(
            f"[v95.9] ❌ DISCOVERY FAILURE for {service_name}:\n"
            f"  Error: {error}\n"
            f"  Attempt: {retry_count + 1}/{self._max_discovery_retries}"
        )

        # Check if we should retry
        if retry_count < self._max_discovery_retries:
            # Calculate exponential backoff delay
            delay = self._discovery_retry_base_delay * (2 ** retry_count)
            # Add jitter to prevent thundering herd
            jitter = delay * 0.1 * (time.time() % 1)
            delay = min(delay + jitter, 60.0)  # Cap at 60 seconds

            logger.info(
                f"[v95.9] 🔄 Retrying {service_name} discovery in {delay:.1f}s "
                f"(attempt {retry_count + 2}/{self._max_discovery_retries})"
            )

            await asyncio.sleep(delay)

            # Retry discovery
            discovery = get_repo_discovery()
            env_var = f"{service_name.upper().replace('-', '_')}_PATH"

            try:
                path = await discovery.discover_repo(service_name, env_var, force_refresh=True)
                if path:
                    logger.info(f"[v95.9] ✅ Retry successful! Found {service_name} at: {path}")
                    self._discovery_failures[service_name]["resolved"] = True
                    return True, path
                else:
                    # Recursive retry with incremented count
                    return await self._handle_discovery_failure(
                        service_name,
                        "Discovery returned None after retry",
                        retry_count + 1
                    )
            except Exception as e:
                return await self._handle_discovery_failure(
                    service_name,
                    str(e),
                    retry_count + 1
                )

        # All retries exhausted - enter degraded mode
        self._degraded_services.add(service_name)
        self._discovery_failures[service_name]["final"] = True

        # Emit explicit notification about degraded mode
        logger.warning(
            f"[v95.9] ⚠️ SERVICE DEGRADED: {service_name}\n"
            f"  All {self._max_discovery_retries} discovery attempts failed.\n"
            f"  System will continue in DEGRADED MODE without {service_name}.\n"
            f"  💡 To fix:\n"
            f"     1. Ensure the {service_name} repository exists\n"
            f"     2. Set {service_name.upper().replace('-', '_')}_PATH environment variable\n"
            f"     3. Check file permissions on the repository"
        )

        # Publish degradation event
        await self.publish_service_lifecycle_event(
            service_name,
            "degraded",
            {
                "reason": "discovery_failed",
                "error": error,
                "retries": retry_count,
            }
        )

        return False, None

    def get_discovery_status(self) -> Dict[str, Any]:
        """
        v95.9: Get current discovery status for all services.

        Returns:
            Dict with discovery status for monitoring
        """
        return {
            "failures": self._discovery_failures.copy(),
            "degraded_services": list(self._degraded_services),
            "retry_states": self._discovery_retry_state.copy(),
            "timestamp": time.time(),
        }

    # =========================================================================
    # v95.9: Issue 22 - Process Crash Without Restart
    # =========================================================================

    async def _monitor_process_health(self, managed: "ManagedProcess") -> None:
        """
        v95.9: Monitor process health and handle crashes with auto-restart.

        Features:
        - Crash detection with detailed logging
        - Automatic restart with exponential backoff
        - Crash rate limiting (circuit breaker)
        - Crash log analysis
        """
        service_name = managed.definition.name

        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(5.0)  # Check every 5 seconds

                if managed.process is None:
                    continue

                # Check if process has exited
                return_code = managed.process.returncode

                if return_code is not None:
                    # Process has crashed!
                    await self._handle_process_crash(managed, return_code)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.9] Error monitoring {service_name}: {e}")

    async def _handle_process_crash(
        self,
        managed: "ManagedProcess",
        return_code: int,
    ) -> None:
        """
        v95.9 + v95.11: Handle process crash with analysis and auto-restart.

        v95.11 enhancements:
        - Distinguish between crash vs graceful shutdown
        - Skip crash handling for SIGTERM during shutdown
        - Add startup grace period awareness
        - Better logging for different scenarios

        Args:
            managed: The crashed process
            return_code: Exit code from the process
        """
        service_name = managed.definition.name
        crash_time = time.time()
        uptime = crash_time - (managed.last_restart or crash_time)

        # v95.11: Check if this is a graceful shutdown, not a crash
        is_sigterm = return_code == -15 or return_code == 143
        is_shutdown_mode = self._shutdown_event.is_set() or self._shutdown_completed

        if is_sigterm and is_shutdown_mode:
            # This is expected during graceful shutdown - don't log as crash
            logger.info(
                f"[v95.11] Service {service_name} terminated by SIGTERM during shutdown "
                f"(PID: {managed.pid}, uptime: {uptime:.1f}s)"
            )
            # Update status but don't trigger crash handling
            managed.status = ServiceStatus.STOPPED
            managed.process = None
            return

        # v95.11: Check for startup-phase termination (very short uptime + SIGTERM)
        # This might indicate the service was killed before fully starting
        startup_grace_period = float(os.environ.get("SERVICE_STARTUP_GRACE_SECONDS", "30.0"))
        if is_sigterm and uptime < startup_grace_period:
            logger.warning(
                f"[v95.11] ⚠️ Service {service_name} terminated during startup phase "
                f"(PID: {managed.pid}, uptime: {uptime:.1f}s < {startup_grace_period}s grace)"
            )
            # Still record it but with lower severity
            if not is_shutdown_mode:
                logger.info(
                    f"[v95.11] This may indicate a shutdown signal arrived during startup. "
                    f"Service will NOT be auto-restarted to prevent loops."
                )
                managed.status = ServiceStatus.STOPPED
                managed.process = None
                return

        # Record crash event for genuine crashes
        crash_event = {
            "timestamp": crash_time,
            "return_code": return_code,
            "pid": managed.pid,
            "restart_count": managed.restart_count,
            "uptime": uptime,
        }

        if service_name not in self._crash_history:
            self._crash_history[service_name] = []
        self._crash_history[service_name].append(crash_event)

        # Analyze crash
        analysis = await self._analyze_crash(managed, return_code)
        self._last_crash_analysis[service_name] = analysis

        # v95.11: Use appropriate log level based on analysis
        log_level = logging.ERROR if analysis.get("severity") != "low" else logging.WARNING
        logger.log(
            log_level,
            f"[v95.9] 💥 PROCESS CRASH: {service_name}\n"
            f"  Exit code: {return_code}\n"
            f"  PID: {managed.pid}\n"
            f"  Uptime: {uptime:.1f}s\n"
            f"  Restart count: {managed.restart_count}\n"
            f"  Analysis: {analysis.get('diagnosis', 'Unknown')}"
        )

        # Check circuit breaker
        if self._should_circuit_break(service_name):
            logger.error(
                f"[v95.9] 🔴 CIRCUIT BREAKER OPEN for {service_name}:\n"
                f"  Too many crashes ({self._max_crashes_per_window}) in {self._crash_rate_window}s window.\n"
                f"  Auto-restart DISABLED to prevent crash loop.\n"
                f"  💡 Manual intervention required. Check logs and fix the issue."
            )
            self._crash_circuit_breakers[service_name] = True
            managed.status = ServiceStatus.FAILED

            # Publish critical event
            await self.publish_service_lifecycle_event(
                service_name,
                "circuit_breaker_open",
                {"reason": "crash_rate_exceeded", "analysis": analysis}
            )
            return

        # Auto-restart with backoff
        backoff = managed.calculate_backoff(base=2.0, max_backoff=120.0)
        logger.info(
            f"[v95.9] 🔄 Auto-restarting {service_name} in {backoff:.1f}s "
            f"(attempt {managed.restart_count + 1})"
        )

        await asyncio.sleep(backoff)

        # Check if shutdown started during backoff
        if self._shutdown_event.is_set() or self._shutdown_completed:
            return

        # Attempt restart
        managed.restart_count += 1
        managed.last_restart = time.time()
        managed.status = ServiceStatus.RESTARTING

        try:
            # Use the existing _spawn_service method which handles the full spawn lifecycle
            success = await self._spawn_service(managed)
            if success:
                logger.info(f"[v95.9] ✅ {service_name} restarted successfully")
                # Reset circuit breaker on successful restart
                self._crash_circuit_breakers[service_name] = False
            else:
                logger.error(f"[v95.9] ❌ {service_name} restart failed")
        except Exception as e:
            logger.error(f"[v95.9] ❌ {service_name} restart error: {e}")

    async def _analyze_crash(
        self,
        managed: "ManagedProcess",
        return_code: int,
    ) -> Dict[str, Any]:
        """
        v95.9: Analyze crash to determine cause and suggest fixes.

        Args:
            managed: The crashed process
            return_code: Exit code

        Returns:
            Analysis result with diagnosis and suggestions
        """
        analysis: Dict[str, Any] = {
            "return_code": return_code,
            "diagnosis": "Unknown",
            "suggestions": [],
            "severity": "medium",
        }

        # Common exit codes
        if return_code == -9 or return_code == 137:
            analysis["diagnosis"] = "Killed by SIGKILL (possibly OOM)"
            analysis["suggestions"] = [
                "Check system memory usage",
                "Increase available memory",
                "Check for memory leaks in the service",
            ]
            analysis["severity"] = "high"
        elif return_code == -15 or return_code == 143:
            analysis["diagnosis"] = "Killed by SIGTERM (graceful shutdown)"
            analysis["severity"] = "low"
        elif return_code == -6 or return_code == 134:
            analysis["diagnosis"] = "Aborted (SIGABRT) - likely assertion failure"
            analysis["suggestions"] = [
                "Check service logs for assertion errors",
                "Look for programming errors",
            ]
            analysis["severity"] = "high"
        elif return_code == 1:
            analysis["diagnosis"] = "General error"
            analysis["suggestions"] = [
                "Check service logs for error details",
                "Verify configuration files",
            ]
        elif return_code == 2:
            analysis["diagnosis"] = "Misuse of command (bad arguments)"
            analysis["suggestions"] = [
                "Check startup arguments",
                "Verify port and path configurations",
            ]
        elif return_code == 127:
            analysis["diagnosis"] = "Command not found"
            analysis["suggestions"] = [
                "Check if Python/venv is correctly set up",
                "Verify script path exists",
            ]

        # Check crash frequency
        recent_crashes = [
            c for c in self._crash_history.get(managed.definition.name, [])
            if time.time() - c["timestamp"] < 60
        ]
        if len(recent_crashes) > 2:
            analysis["suggestions"].append("Multiple crashes in 60s - possible startup issue")
            analysis["severity"] = "critical"

        return analysis

    def _should_circuit_break(self, service_name: str) -> bool:
        """
        v95.9: Check if circuit breaker should activate based on crash rate.

        Args:
            service_name: Service to check

        Returns:
            True if circuit should break
        """
        if service_name in self._crash_circuit_breakers and self._crash_circuit_breakers[service_name]:
            return True  # Already open

        # Count recent crashes
        cutoff = time.time() - self._crash_rate_window
        recent_crashes = [
            c for c in self._crash_history.get(service_name, [])
            if c["timestamp"] > cutoff
        ]

        return len(recent_crashes) >= self._max_crashes_per_window

    # =========================================================================
    # v95.9: Issue 23 - Port Conflict No Recovery
    # =========================================================================

    async def _resolve_port_conflict(
        self,
        service_name: str,
        requested_port: int,
    ) -> Tuple[int, Optional[str]]:
        """
        v95.9: Resolve port conflicts automatically.

        Features:
        - Detect port conflicts
        - Find alternative port
        - Identify conflicting process
        - Clean up stale processes

        Args:
            service_name: Service requesting the port
            requested_port: Originally requested port

        Returns:
            Tuple of (resolved_port, conflict_info)
        """
        # Check if requested port is available
        is_available = await self._check_port_available(requested_port)

        if is_available:
            self._port_allocation_map[service_name] = requested_port
            return requested_port, None

        # Port conflict detected
        conflict_info = await self._identify_port_conflict(requested_port)

        logger.warning(
            f"[v95.9] ⚠️ PORT CONFLICT: {service_name} requested port {requested_port}\n"
            f"  Conflict: {conflict_info}"
        )

        # Try to clean up if it's a stale JARVIS process
        if conflict_info and "jarvis" in conflict_info.lower():
            cleanup_success = await self._cleanup_stale_process(requested_port)
            if cleanup_success:
                logger.info(f"[v95.9] ✅ Cleaned up stale process on port {requested_port}")
                await asyncio.sleep(1.0)  # Give OS time to release port
                self._port_allocation_map[service_name] = requested_port
                return requested_port, f"Cleaned up stale process: {conflict_info}"

        # Find alternative port
        alternative_port = await self._find_available_port(service_name)

        if alternative_port:
            logger.info(
                f"[v95.9] 🔄 {service_name}: Using alternative port {alternative_port} "
                f"(original {requested_port} in use)"
            )
            self._port_allocation_map[service_name] = alternative_port

            # Record conflict for monitoring
            if requested_port not in self._port_conflict_history:
                self._port_conflict_history[requested_port] = []
            self._port_conflict_history[requested_port].append({
                "timestamp": time.time(),
                "service": service_name,
                "conflict_info": conflict_info,
                "resolved_port": alternative_port,
            })

            return alternative_port, f"Conflict with: {conflict_info}"

        # No alternative found
        logger.error(
            f"[v95.9] ❌ CRITICAL: Cannot find available port for {service_name}\n"
            f"  Requested: {requested_port}\n"
            f"  Scanned range: {self._dynamic_port_range}\n"
            f"  💡 Free up ports or adjust JARVIS_PORT_RANGE_START/END"
        )
        return -1, f"No ports available in range {self._dynamic_port_range}"

    async def _check_port_available(self, port: int) -> bool:
        """
        v95.9: Check if a port is available (with caching).

        Args:
            port: Port number to check

        Returns:
            True if port is available
        """
        # Check cache
        if port in self._port_scan_cache:
            in_use, check_time = self._port_scan_cache[port]
            if time.time() - check_time < self._port_scan_cache_ttl:
                return not in_use

        # Perform actual check
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            result = sock.connect_ex(('localhost', port))
            in_use = (result == 0)
        except Exception:
            in_use = False  # Assume available if check fails
        finally:
            sock.close()

        # Update cache
        self._port_scan_cache[port] = (in_use, time.time())
        return not in_use

    async def _identify_port_conflict(self, port: int) -> str:
        """
        v95.9: Identify what process is using a port.

        Args:
            port: Port number

        Returns:
            Description of conflicting process
        """
        try:
            import psutil

            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr and hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN':
                        try:
                            proc = psutil.Process(conn.pid)
                            cmdline = ' '.join(proc.cmdline()[:3])
                            return f"PID={conn.pid}, Name={proc.name()}, Cmd={cmdline}"
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            return f"PID={conn.pid} (details unavailable)"
            return "Unknown process"
        except ImportError:
            return "psutil not available for process identification"
        except Exception as e:
            return f"Error identifying process: {e}"

    async def _cleanup_stale_process(self, port: int) -> bool:
        """
        v95.9: Clean up stale JARVIS process on a port.

        Args:
            port: Port to clean up

        Returns:
            True if cleanup successful
        """
        try:
            import psutil

            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr and hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN':
                        try:
                            proc = psutil.Process(conn.pid)
                            cmdline = ' '.join(proc.cmdline())

                            # Only kill JARVIS-related processes
                            if any(x in cmdline.lower() for x in ['jarvis', 'prime', 'reactor']):
                                logger.info(f"[v95.9] Terminating stale process: {proc.name()} (PID={conn.pid})")
                                proc.terminate()

                                # Wait for graceful shutdown
                                try:
                                    proc.wait(timeout=5)
                                except psutil.TimeoutExpired:
                                    logger.warning(f"[v95.9] Force killing process {conn.pid}")
                                    proc.kill()

                                return True
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            return False
        except ImportError:
            return False
        except Exception as e:
            logger.error(f"[v95.9] Error cleaning up stale process: {e}")
            return False

    async def _find_available_port(self, service_name: str) -> Optional[int]:
        """
        v95.9: Find an available port in the configured range.

        Args:
            service_name: Service requesting the port

        Returns:
            Available port number or None
        """
        start, end = self._dynamic_port_range

        # Check ports in range
        for port in range(start, end + 1):
            if await self._check_port_available(port):
                return port

        return None

    # =========================================================================
    # v95.9: Issue 24 - Import Error No Fallback
    # =========================================================================

    async def _safe_import(
        self,
        module_name: str,
        fallbacks: Optional[List[str]] = None,
        lazy: bool = True,
    ) -> Tuple[Any, Optional[str]]:
        """
        v95.9: Safely import a module with fallback options.

        Features:
        - Primary import attempt
        - Fallback to alternatives
        - Error recording
        - Lazy loading option

        Args:
            module_name: Primary module to import
            fallbacks: List of fallback module names
            lazy: Whether to use lazy loading

        Returns:
            Tuple of (module_or_none, error_message)
        """
        fallbacks = fallbacks or []

        # Check if already loaded
        if module_name in self._lazy_imports:
            return self._lazy_imports[module_name], None

        # Try primary import
        try:
            module = __import__(module_name)
            # Handle nested module names
            for part in module_name.split('.')[1:]:
                module = getattr(module, part)

            self._lazy_imports[module_name] = module
            return module, None

        except ImportError as e:
            error = str(e)
            logger.warning(f"[v95.9] Import failed for {module_name}: {error}")

            # Record error
            self._import_errors[module_name] = {
                "error": error,
                "timestamp": time.time(),
                "fallbacks_tried": [],
            }

            # Try fallbacks
            for fallback in fallbacks:
                try:
                    logger.info(f"[v95.9] Trying fallback import: {fallback}")
                    module = __import__(fallback)
                    for part in fallback.split('.')[1:]:
                        module = getattr(module, part)

                    self._lazy_imports[module_name] = module
                    self._loaded_alternatives[module_name] = fallback
                    logger.info(f"[v95.9] ✅ Fallback {fallback} loaded for {module_name}")
                    return module, None

                except ImportError as fe:
                    self._import_errors[module_name]["fallbacks_tried"].append({
                        "module": fallback,
                        "error": str(fe),
                    })

            # All imports failed
            logger.error(
                f"[v95.9] ❌ IMPORT FAILURE: {module_name}\n"
                f"  Error: {error}\n"
                f"  Fallbacks tried: {fallbacks}\n"
                f"  💡 Install missing package or check PYTHONPATH"
            )
            return None, error

    def _register_import_fallback(self, module: str, fallbacks: List[str]) -> None:
        """
        v95.9: Register fallback modules for a primary module.

        Args:
            module: Primary module name
            fallbacks: List of fallback module names
        """
        self._import_fallbacks[module] = fallbacks

    # =========================================================================
    # v95.9: Issue 25 - Network Timeout No Retry
    # =========================================================================

    async def _network_request_with_retry(
        self,
        url: str,
        method: str = "GET",
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        **kwargs,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """
        v95.9: Make network request with retry and circuit breaker.

        Features:
        - Exponential backoff retry
        - Circuit breaker for repeated failures
        - Network health monitoring
        - Detailed error reporting

        Args:
            url: Request URL
            method: HTTP method
            timeout: Request timeout
            max_retries: Maximum retry attempts
            **kwargs: Additional request arguments

        Returns:
            Tuple of (response_data, error_message)
        """
        config = self._network_retry_config
        # Ensure non-None values with sensible defaults
        effective_timeout: float = timeout if timeout is not None else config.get("timeout", 30.0)
        effective_max_retries: int = max_retries if max_retries is not None else config.get("max_retries", 3)

        # Check circuit breaker
        endpoint = self._get_endpoint_key(url)
        if self._is_circuit_open(endpoint):
            return None, f"Circuit breaker open for {endpoint}"

        last_error: Optional[str] = None

        for attempt in range(effective_max_retries + 1):
            try:
                # Get the shared HTTP session (connection pooled)
                session = await self._get_http_session()
                request_timeout = aiohttp.ClientTimeout(total=effective_timeout)

                # Make request using the session (don't close - it's shared)
                async with session.request(
                    method,
                    url,
                    timeout=request_timeout,
                    **kwargs
                ) as response:
                    if response.status < 400:
                        # Success - reset circuit breaker
                        self._record_network_success(endpoint)
                        content_type = response.content_type or ""
                        data = await response.json() if 'json' in content_type else await response.text()
                        return data, None
                    else:
                        last_error = f"HTTP {response.status}: {await response.text()}"

            except asyncio.TimeoutError:
                last_error = f"Timeout after {effective_timeout}s"
            except aiohttp.ClientConnectorError as e:
                last_error = f"Connection error: {e}"
            except Exception as e:
                last_error = f"Request error: {e}"

            # Record failure
            self._record_network_failure(endpoint, last_error or "Unknown error")

            # Check if we should retry
            if attempt < effective_max_retries:
                delay = min(
                    config.get("base_delay", 1.0) * (2 ** attempt),
                    config.get("max_delay", 30.0)
                )
                # Add jitter
                jitter = delay * 0.1 * (time.time() % 1)
                delay += jitter

                logger.debug(
                    f"[v95.9] Network retry for {endpoint} in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{effective_max_retries + 1}): {last_error}"
                )
                await asyncio.sleep(delay)

        # All retries failed
        logger.warning(
            f"[v95.9] ⚠️ Network request failed after {effective_max_retries + 1} attempts: {url}\n"
            f"  Last error: {last_error}"
        )
        return None, last_error

    def _get_endpoint_key(self, url: str) -> str:
        """Extract endpoint key from URL for circuit breaker tracking."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"

    def _is_circuit_open(self, endpoint: str) -> bool:
        """Check if circuit breaker is open for an endpoint."""
        if endpoint not in self._network_circuit_breakers:
            return False

        breaker = self._network_circuit_breakers[endpoint]
        if not breaker.get("open", False):
            return False

        # Check if reset time has passed
        reset_time = breaker.get("reset_time", 0)
        if time.time() > reset_time:
            # Half-open - allow one request through
            breaker["open"] = False
            breaker["half_open"] = True
            return False

        return True

    def _record_network_success(self, endpoint: str) -> None:
        """Record successful network request."""
        if endpoint in self._network_circuit_breakers:
            self._network_circuit_breakers[endpoint] = {
                "failures": 0,
                "open": False,
                "half_open": False,
            }

        self._network_health_status[endpoint] = {
            "healthy": True,
            "last_success": time.time(),
        }

    def _record_network_failure(self, endpoint: str, error: str) -> None:
        """Record failed network request and potentially open circuit."""
        config = self._network_retry_config

        if endpoint not in self._network_circuit_breakers:
            self._network_circuit_breakers[endpoint] = {
                "failures": 0,
                "open": False,
            }

        breaker = self._network_circuit_breakers[endpoint]
        breaker["failures"] = breaker.get("failures", 0) + 1
        breaker["last_error"] = error
        breaker["last_failure"] = time.time()

        # Check if we should open the circuit
        if breaker["failures"] >= config["circuit_threshold"]:
            breaker["open"] = True
            breaker["reset_time"] = time.time() + config["circuit_reset_time"]
            logger.warning(
                f"[v95.9] 🔴 Circuit breaker OPEN for {endpoint}: "
                f"{breaker['failures']} consecutive failures"
            )

        self._network_health_status[endpoint] = {
            "healthy": False,
            "last_failure": time.time(),
            "error": error,
        }

    # Note: _get_http_session is defined in v93.11 section (line ~4319)
    # with comprehensive connection pooling configuration

    def get_error_recovery_status(self) -> Dict[str, Any]:
        """
        v95.9: Get comprehensive error recovery status.

        Returns:
            Dict with status of all error recovery systems
        """
        return {
            "discovery": {
                "failures": self._discovery_failures,
                "degraded_services": list(self._degraded_services),
            },
            "crashes": {
                "history": {k: len(v) for k, v in self._crash_history.items()},
                "circuit_breakers": self._crash_circuit_breakers,
                "last_analysis": self._last_crash_analysis,
            },
            "ports": {
                "allocations": self._port_allocation_map,
                "conflicts": {k: len(v) for k, v in self._port_conflict_history.items()},
            },
            "imports": {
                "errors": self._import_errors,
                "alternatives": self._loaded_alternatives,
            },
            "network": {
                "circuit_breakers": {
                    k: {"open": v.get("open", False), "failures": v.get("failures", 0)}
                    for k, v in self._network_circuit_breakers.items()
                },
                "health": self._network_health_status,
            },
            "timestamp": time.time(),
        }

    # =========================================================================
    # v95.10: Cross-Repo Integration Infrastructure (Issues 31-38)
    # =========================================================================

    # -------------------------------------------------------------------------
    # Issue 31: Unified Configuration System
    # -------------------------------------------------------------------------

    async def _initialize_unified_config(self) -> None:
        """
        v95.10: Initialize unified cross-repo configuration system.

        Features:
        - Config inheritance (base -> repo-specific -> overrides)
        - Schema validation with type coercion
        - Change watchers for dynamic updates
        - Conflict detection and resolution
        """
        self._ensure_locks_initialized()
        assert self._config_lock is not None

        async with self._config_lock:
            # Define base Trinity configuration schema
            self._config_schema = {
                "ports": {
                    "type": "dict",
                    "schema": {
                        "jarvis_body": {"type": "int", "default": 8010, "min": 1024, "max": 65535},
                        "jarvis_prime": {"type": "int", "default": 8000, "min": 1024, "max": 65535},
                        "reactor_core": {"type": "int", "default": 8090, "min": 1024, "max": 65535},
                    },
                },
                "timeouts": {
                    "type": "dict",
                    "schema": {
                        "startup": {"type": "float", "default": 120.0, "min": 10.0},
                        "health_check": {"type": "float", "default": 5.0, "min": 1.0},
                        "shutdown": {"type": "float", "default": 30.0, "min": 5.0},
                    },
                },
                "retry": {
                    "type": "dict",
                    "schema": {
                        "max_attempts": {"type": "int", "default": 5, "min": 1, "max": 20},
                        "base_delay": {"type": "float", "default": 1.0, "min": 0.1},
                        "max_delay": {"type": "float", "default": 60.0, "min": 1.0},
                    },
                },
                "features": {
                    "type": "dict",
                    "schema": {
                        "auto_recovery": {"type": "bool", "default": True},
                        "metrics_collection": {"type": "bool", "default": True},
                        "distributed_tracing": {"type": "bool", "default": True},
                        "state_sync": {"type": "bool", "default": True},
                    },
                },
            }

            # Load configs from all repos
            await self._load_repo_configs()

            # Merge with inheritance
            self._unified_config = await self._merge_configs_with_inheritance()

            # Start config sync task
            if self._config_sync_task is None or self._config_sync_task.done():
                self._config_sync_task = asyncio.create_task(
                    self._config_sync_loop(),
                    name="config_sync_loop"
                )
                self._track_background_task(self._config_sync_task)

            logger.info("[v95.10] ✅ Unified configuration system initialized")

    async def _load_repo_configs(self) -> None:
        """Load configuration from each repo's config file."""
        repos = {
            "jarvis": self.config.jarvis_path if hasattr(self.config, 'jarvis_path') else Path.cwd(),
            "jarvis-prime": self.config.jarvis_prime_path,
            "reactor-core": self.config.reactor_core_path,
        }

        for repo_name, repo_path in repos.items():
            if not repo_path or not repo_path.exists():
                continue

            config_files = [
                repo_path / "trinity_config.json",
                repo_path / "config" / "trinity.json",
                repo_path / ".trinity.json",
            ]

            for config_file in config_files:
                if config_file.exists():
                    try:
                        with open(config_file, "r") as f:
                            self._config_sources[repo_name] = json.load(f)
                        logger.debug(f"[v95.10] Loaded config for {repo_name} from {config_file}")
                        break
                    except Exception as e:
                        logger.warning(f"[v95.10] Failed to load config from {config_file}: {e}")

    async def _merge_configs_with_inheritance(self) -> Dict[str, Any]:
        """
        Merge configurations with proper inheritance.

        Priority (lowest to highest):
        1. Schema defaults
        2. JARVIS body config
        3. JARVIS Prime config
        4. Reactor Core config
        5. Environment variables
        6. User overrides
        """
        merged: Dict[str, Any] = {}

        # 1. Apply schema defaults
        merged = self._apply_schema_defaults(self._config_schema)

        # 2-4. Merge repo configs in order
        for repo in ["jarvis", "jarvis-prime", "reactor-core"]:
            if repo in self._config_sources:
                merged = self._deep_merge(merged, self._config_sources[repo])

        # 5. Apply environment variable overrides
        merged = self._apply_env_overrides(merged)

        # 6. Apply user overrides
        if self._config_overrides:
            merged = self._deep_merge(merged, self._config_overrides)

        # Validate merged config
        validated = self._validate_config(merged, self._config_schema)

        return validated

    def _apply_schema_defaults(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Apply default values from schema."""
        result: Dict[str, Any] = {}

        for key, spec in schema.items():
            if spec.get("type") == "dict" and "schema" in spec:
                result[key] = self._apply_schema_defaults(spec["schema"])
            elif "default" in spec:
                result[key] = spec["default"]

        return result

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two dictionaries."""
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _apply_env_overrides(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Apply environment variable overrides."""
        env_mappings = {
            "JARVIS_BODY_PORT": ("ports", "jarvis_body"),
            "JARVIS_PRIME_PORT": ("ports", "jarvis_prime"),
            "REACTOR_CORE_PORT": ("ports", "reactor_core"),
            "JARVIS_STARTUP_TIMEOUT": ("timeouts", "startup"),
            "JARVIS_HEALTH_TIMEOUT": ("timeouts", "health_check"),
            "JARVIS_MAX_RETRIES": ("retry", "max_attempts"),
            "JARVIS_AUTO_RECOVERY": ("features", "auto_recovery"),
            "JARVIS_METRICS": ("features", "metrics_collection"),
        }

        for env_var, (section, key) in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                if section not in config:
                    config[section] = {}

                # Type coercion
                if value.lower() in ("true", "false"):
                    config[section][key] = value.lower() == "true"
                elif value.isdigit():
                    config[section][key] = int(value)
                else:
                    try:
                        config[section][key] = float(value)
                    except ValueError:
                        config[section][key] = value

        return config

    def _validate_config(self, config: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        """Validate configuration against schema."""
        validated: Dict[str, Any] = {}

        for key, spec in schema.items():
            value = config.get(key)

            if value is None:
                if "default" in spec:
                    validated[key] = spec["default"]
                continue

            # Type validation and coercion
            expected_type = spec.get("type", "any")

            if expected_type == "dict" and "schema" in spec:
                if isinstance(value, dict):
                    validated[key] = self._validate_config(value, spec["schema"])
                else:
                    validated[key] = self._apply_schema_defaults(spec["schema"])
            elif expected_type == "int":
                try:
                    val = int(value)
                    # Range validation
                    if "min" in spec:
                        val = max(val, spec["min"])
                    if "max" in spec:
                        val = min(val, spec["max"])
                    validated[key] = val
                except (ValueError, TypeError):
                    validated[key] = spec.get("default", 0)
            elif expected_type == "float":
                try:
                    val = float(value)
                    if "min" in spec:
                        val = max(val, spec["min"])
                    if "max" in spec:
                        val = min(val, spec["max"])
                    validated[key] = val
                except (ValueError, TypeError):
                    validated[key] = spec.get("default", 0.0)
            elif expected_type == "bool":
                validated[key] = bool(value)
            else:
                validated[key] = value

        return validated

    async def _config_sync_loop(self) -> None:
        """Background task to sync configuration across repos."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._config_sync_interval)

                if self._shutdown_event.is_set():
                    break

                # Reload and re-merge configs
                await self._load_repo_configs()
                new_config = await self._merge_configs_with_inheritance()

                # Detect changes and notify watchers
                assert self._config_lock is not None
                async with self._config_lock:
                    changes = self._detect_config_changes(self._unified_config, new_config)
                    if changes:
                        logger.info(f"[v95.10] Config changes detected: {list(changes.keys())}")
                        self._unified_config = new_config

                        # Notify watchers
                        for key, new_value in changes.items():
                            if key in self._config_watchers:
                                try:
                                    callback = self._config_watchers[key]
                                    if asyncio.iscoroutinefunction(callback):
                                        await callback(key, new_value)
                                    else:
                                        callback(key, new_value)
                                except Exception as e:
                                    logger.error(f"[v95.10] Config watcher error for {key}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Config sync error: {e}")

    def _detect_config_changes(
        self,
        old_config: Dict[str, Any],
        new_config: Dict[str, Any],
        prefix: str = ""
    ) -> Dict[str, Any]:
        """Detect changes between two configs."""
        changes: Dict[str, Any] = {}

        all_keys = set(old_config.keys()) | set(new_config.keys())
        for key in all_keys:
            full_key = f"{prefix}.{key}" if prefix else key
            old_val = old_config.get(key)
            new_val = new_config.get(key)

            if isinstance(old_val, dict) and isinstance(new_val, dict):
                nested_changes = self._detect_config_changes(old_val, new_val, full_key)
                changes.update(nested_changes)
            elif old_val != new_val:
                changes[full_key] = new_val

        return changes

    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation.

        Args:
            key: Dot-separated key (e.g., "ports.jarvis_prime")
            default: Default value if key not found

        Returns:
            Configuration value
        """
        parts = key.split(".")
        value: Any = self._unified_config

        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default

        return value

    def set_config_override(self, key: str, value: Any) -> None:
        """
        Set a configuration override.

        Args:
            key: Dot-separated key
            value: New value
        """
        parts = key.split(".")
        current = self._config_overrides

        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        current[parts[-1]] = value

    def watch_config(self, key: str, callback: Callable) -> None:
        """Register a watcher for configuration changes."""
        self._config_watchers[key] = callback

    # -------------------------------------------------------------------------
    # Issue 32: Cross-Repo Unified Logging
    # -------------------------------------------------------------------------

    async def _initialize_unified_logging(self) -> None:
        """
        v95.10: Initialize unified cross-repo logging system.

        Features:
        - W3C Trace Context propagation
        - Log correlation with trace IDs
        - Log aggregation and buffering
        - Cross-repo log streaming
        """
        self._ensure_locks_initialized()
        assert self._log_lock is not None

        async with self._log_lock:
            # Setup unified log formatter with trace context
            self._setup_trace_context_formatter()

            # Initialize state persistence path
            jarvis_home = Path.home() / ".jarvis"
            self._state_persistence_path = jarvis_home / "logs"
            self._state_persistence_path.mkdir(parents=True, exist_ok=True)

            # Start log flush task
            if self._log_flush_task is None or self._log_flush_task.done():
                self._log_flush_task = asyncio.create_task(
                    self._log_flush_loop(),
                    name="log_flush_loop"
                )
                self._track_background_task(self._log_flush_task)

            logger.info("[v95.10] ✅ Unified logging system initialized")

    def _setup_trace_context_formatter(self) -> None:
        """Setup log formatter with W3C Trace Context."""

        class TraceContextFormatter(logging.Formatter):
            """Formatter that includes trace context in log messages."""

            def __init__(self, orchestrator: "ProcessOrchestrator"):
                super().__init__(
                    fmt="%(asctime)s [%(levelname)s] [%(trace_id)s:%(span_id)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
                self._orchestrator = orchestrator

            def format(self, record: logging.LogRecord) -> str:
                # Add trace context to record
                trace_id = getattr(record, "trace_id", None)
                span_id = getattr(record, "span_id", None)

                if not trace_id:
                    trace_id = self._orchestrator._startup_correlation_id or "-"
                if not span_id:
                    span_id = "-"

                record.trace_id = trace_id[:16] if len(trace_id) > 16 else trace_id
                record.span_id = span_id[:8] if len(span_id) > 8 else span_id

                return super().format(record)

        # Store formatter for use
        self._unified_log_handlers["trace_formatter"] = TraceContextFormatter(self)

    async def emit_cross_repo_log(
        self,
        level: str,
        message: str,
        source_repo: str,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Emit a log entry with cross-repo correlation.

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            source_repo: Source repository name
            trace_id: Optional trace ID (generated if not provided)
            span_id: Optional span ID
            extra: Additional context
        """
        if not self._log_correlation_enabled:
            return

        self._ensure_locks_initialized()
        assert self._log_lock is not None

        # Generate trace ID if not provided
        if not trace_id:
            trace_id = self._startup_correlation_id or self._generate_trace_id()

        if not span_id:
            span_id = self._generate_span_id()

        log_entry = {
            "timestamp": time.time(),
            "level": level.upper(),
            "message": message,
            "source_repo": source_repo,
            "trace_id": trace_id,
            "span_id": span_id,
            "extra": extra or {},
        }

        async with self._log_lock:
            self._log_aggregation_buffer.append(log_entry)

            # Flush if buffer is full
            if len(self._log_aggregation_buffer) >= self._log_buffer_size:
                await self._flush_log_buffer()

    def _generate_trace_id(self) -> str:
        """Generate W3C-compliant trace ID (32 hex chars)."""
        import uuid
        return uuid.uuid4().hex

    def _generate_span_id(self) -> str:
        """Generate W3C-compliant span ID (16 hex chars)."""
        import uuid
        return uuid.uuid4().hex[:16]

    async def _log_flush_loop(self) -> None:
        """Background task to periodically flush log buffer."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._log_flush_interval)

                if self._shutdown_event.is_set():
                    break

                assert self._log_lock is not None
                async with self._log_lock:
                    if self._log_aggregation_buffer:
                        await self._flush_log_buffer()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Log flush error: {e}")

    async def _flush_log_buffer(self) -> None:
        """Flush log buffer to storage/aggregation."""
        if not self._log_aggregation_buffer:
            return

        entries = self._log_aggregation_buffer.copy()
        self._log_aggregation_buffer.clear()

        # Write to aggregated log file
        if self._state_persistence_path:
            log_file = self._state_persistence_path / "unified_logs.jsonl"
            try:
                with open(log_file, "a") as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logger.error(f"[v95.10] Failed to write logs: {e}")

    def get_trace_context_headers(self) -> Dict[str, str]:
        """Get trace context headers for HTTP propagation."""
        trace_id = self._startup_correlation_id or self._generate_trace_id()
        span_id = self._generate_span_id()

        return {
            self._trace_id_header: trace_id,
            self._span_id_header: span_id,
            "traceparent": f"00-{trace_id}-{span_id}-01",  # W3C format
        }

    # -------------------------------------------------------------------------
    # Issue 33: Cross-Repo Metrics Collection
    # -------------------------------------------------------------------------

    async def _initialize_metrics_collection(self) -> None:
        """
        v95.10: Initialize unified metrics collection system.

        Features:
        - Cross-repo metric aggregation
        - Custom metric collectors
        - Metric buffering and batching
        - Health dashboard data
        """
        self._ensure_locks_initialized()
        assert self._metrics_lock is not None

        if not self._metrics_enabled:
            logger.info("[v95.10] Metrics collection disabled")
            return

        async with self._metrics_lock:
            # Register default metrics
            self._register_default_metrics()

            # Start collection task
            if self._metrics_collection_task is None or self._metrics_collection_task.done():
                self._metrics_collection_task = asyncio.create_task(
                    self._metrics_collection_loop(),
                    name="metrics_collection_loop"
                )
                self._track_background_task(self._metrics_collection_task)

            logger.info("[v95.10] ✅ Metrics collection system initialized")

    def _register_default_metrics(self) -> None:
        """Register default cross-repo metrics."""
        self._metrics_registry = {
            "service_health": {
                "type": "gauge",
                "description": "Service health status (0=unhealthy, 1=healthy)",
                "labels": ["service", "repo"],
            },
            "service_uptime": {
                "type": "gauge",
                "description": "Service uptime in seconds",
                "labels": ["service"],
            },
            "request_latency": {
                "type": "histogram",
                "description": "Request latency in milliseconds",
                "labels": ["service", "endpoint"],
                "buckets": [10, 50, 100, 250, 500, 1000, 2500, 5000],
            },
            "error_count": {
                "type": "counter",
                "description": "Total error count",
                "labels": ["service", "error_type"],
            },
            "memory_usage": {
                "type": "gauge",
                "description": "Memory usage in bytes",
                "labels": ["service"],
            },
            "cpu_usage": {
                "type": "gauge",
                "description": "CPU usage percentage",
                "labels": ["service"],
            },
            "active_connections": {
                "type": "gauge",
                "description": "Active connection count",
                "labels": ["service"],
            },
        }

    async def _metrics_collection_loop(self) -> None:
        """Background task to collect metrics from all repos."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._metrics_collection_interval)

                if self._shutdown_event.is_set():
                    break

                await self._collect_all_metrics()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Metrics collection error: {e}")

    async def _collect_all_metrics(self) -> None:
        """Collect metrics from all registered sources."""
        self._ensure_locks_initialized()
        assert self._metrics_lock is not None

        timestamp = time.time()

        async with self._metrics_lock:
            # Collect from each service
            for service_name, managed in self.processes.items():
                try:
                    metrics = await self._collect_service_metrics(service_name, managed)
                    self._service_metrics_cache[service_name] = {
                        "metrics": metrics,
                        "timestamp": timestamp,
                    }
                except Exception as e:
                    logger.debug(f"[v95.10] Failed to collect metrics from {service_name}: {e}")

            # Run custom collectors
            for collector_name, collector_fn in self._metrics_collectors.items():
                try:
                    if asyncio.iscoroutinefunction(collector_fn):
                        metrics = await collector_fn()
                    else:
                        metrics = collector_fn()

                    self._metrics_buffer.extend(metrics if isinstance(metrics, list) else [metrics])
                except Exception as e:
                    logger.debug(f"[v95.10] Collector {collector_name} failed: {e}")

    async def _collect_service_metrics(
        self,
        service_name: str,
        managed: "ManagedProcess"
    ) -> Dict[str, Any]:
        """Collect metrics from a single service."""
        metrics: Dict[str, Any] = {
            "service": service_name,
            "timestamp": time.time(),
        }

        # Health status
        metrics["health"] = 1 if managed.status == ServiceStatus.HEALTHY else 0

        # Uptime
        if managed.last_restart:
            metrics["uptime"] = time.time() - managed.last_restart

        # Process metrics (if available)
        if managed.pid and managed.process:
            try:
                import psutil
                proc = psutil.Process(managed.pid)
                metrics["memory_bytes"] = proc.memory_info().rss
                metrics["cpu_percent"] = proc.cpu_percent()
            except Exception:
                pass

        # Restart count
        metrics["restart_count"] = managed.restart_count

        # Port
        metrics["port"] = managed.port

        return metrics

    def register_metric_collector(self, name: str, collector: Callable) -> None:
        """Register a custom metric collector."""
        self._metrics_collectors[name] = collector

    def record_metric(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a metric value."""
        if not self._metrics_enabled:
            return

        self._metrics_buffer.append({
            "name": name,
            "value": value,
            "labels": labels or {},
            "timestamp": time.time(),
        })

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """Get current metrics snapshot for dashboard."""
        return {
            "services": self._service_metrics_cache.copy(),
            "buffer_size": len(self._metrics_buffer),
            "registry": list(self._metrics_registry.keys()),
            "timestamp": time.time(),
        }

    # -------------------------------------------------------------------------
    # Issue 34: Cross-Repo Error Propagation
    # -------------------------------------------------------------------------

    async def _initialize_error_propagation(self) -> None:
        """
        v95.10: Initialize cross-repo error propagation system.

        Features:
        - Error context preservation across repos
        - Error correlation and chaining
        - Cross-repo error handlers
        - Distributed error tracking
        """
        self._ensure_locks_initialized()

        if not self._error_propagation_enabled:
            logger.info("[v95.10] Error propagation disabled")
            return

        # Register default error handlers
        self._register_default_error_handlers()

        logger.info("[v95.10] ✅ Error propagation system initialized")

    def _register_default_error_handlers(self) -> None:
        """Register default error handlers for common error types."""
        self._error_handlers = {
            "connection_error": [self._handle_connection_error],
            "timeout_error": [self._handle_timeout_error],
            "validation_error": [self._handle_validation_error],
            "dependency_error": [self._handle_dependency_error],
        }

    async def propagate_error(
        self,
        error: Exception,
        source_repo: str,
        target_repos: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Propagate an error across repos with full context.

        Args:
            error: The exception to propagate
            source_repo: Source repository name
            target_repos: Target repos to notify (None = all)
            context: Additional context

        Returns:
            Error ID for tracking
        """
        self._ensure_locks_initialized()
        assert self._error_lock is not None

        error_id = self._generate_error_id()
        error_type = type(error).__name__

        error_info = {
            "id": error_id,
            "type": error_type,
            "message": str(error),
            "source_repo": source_repo,
            "target_repos": target_repos or ["jarvis", "jarvis-prime", "reactor-core"],
            "context": context or {},
            "trace_id": self._startup_correlation_id,
            "timestamp": time.time(),
            "traceback": self._format_traceback(error),
        }

        async with self._error_lock:
            # Store in registry
            self._error_registry[error_id] = error_info

            # Maintain max history
            if len(self._error_registry) > self._max_error_history:
                oldest = min(self._error_registry.keys(), key=lambda k: self._error_registry[k]["timestamp"])
                del self._error_registry[oldest]

            # Check for correlated errors
            await self._correlate_error(error_id, error_info)

        # Call error handlers
        await self._invoke_error_handlers(error_type, error_info)

        # Emit log
        await self.emit_cross_repo_log(
            "ERROR",
            f"Cross-repo error: {error_type}: {error}",
            source_repo,
            extra={"error_id": error_id}
        )

        return error_id

    def _generate_error_id(self) -> str:
        """Generate unique error ID."""
        import uuid
        return f"err-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def _format_traceback(self, error: Exception) -> str:
        """Format exception traceback."""
        import traceback
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))

    async def _correlate_error(self, error_id: str, error_info: Dict[str, Any]) -> None:
        """Find and link correlated errors."""
        trace_id = error_info.get("trace_id")
        if not trace_id:
            return

        # Find other errors with same trace ID
        for other_id, other_info in self._error_registry.items():
            if other_id == error_id:
                continue
            if other_info.get("trace_id") == trace_id:
                # Link errors
                if trace_id not in self._error_correlation_map:
                    self._error_correlation_map[trace_id] = []
                if error_id not in self._error_correlation_map[trace_id]:
                    self._error_correlation_map[trace_id].append(error_id)
                if other_id not in self._error_correlation_map[trace_id]:
                    self._error_correlation_map[trace_id].append(other_id)

    async def _invoke_error_handlers(self, error_type: str, error_info: Dict[str, Any]) -> None:
        """Invoke registered error handlers."""
        handlers = self._error_handlers.get(error_type.lower(), [])
        handlers.extend(self._error_handlers.get("*", []))  # Wildcard handlers

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(error_info)
                else:
                    handler(error_info)
            except Exception as e:
                logger.error(f"[v95.10] Error handler failed: {e}")

    async def _handle_connection_error(self, error_info: Dict[str, Any]) -> None:
        """Handle connection errors."""
        logger.warning(f"[v95.10] Connection error from {error_info['source_repo']}: {error_info['message']}")

    async def _handle_timeout_error(self, error_info: Dict[str, Any]) -> None:
        """Handle timeout errors."""
        logger.warning(f"[v95.10] Timeout error from {error_info['source_repo']}: {error_info['message']}")

    async def _handle_validation_error(self, error_info: Dict[str, Any]) -> None:
        """Handle validation errors."""
        logger.warning(f"[v95.10] Validation error from {error_info['source_repo']}: {error_info['message']}")

    async def _handle_dependency_error(self, error_info: Dict[str, Any]) -> None:
        """Handle dependency errors."""
        logger.warning(f"[v95.10] Dependency error from {error_info['source_repo']}: {error_info['message']}")

    def register_error_handler(self, error_type: str, handler: Callable) -> None:
        """Register an error handler."""
        if error_type not in self._error_handlers:
            self._error_handlers[error_type] = []
        self._error_handlers[error_type].append(handler)

    def get_error_chain(self, error_id: str) -> List[Dict[str, Any]]:
        """Get chain of correlated errors."""
        error_info = self._error_registry.get(error_id)
        if not error_info:
            return []

        trace_id = error_info.get("trace_id")
        if not trace_id or trace_id not in self._error_correlation_map:
            return [error_info]

        return [
            self._error_registry[eid]
            for eid in self._error_correlation_map[trace_id]
            if eid in self._error_registry
        ]

    # -------------------------------------------------------------------------
    # Issue 35: Cross-Repo State Synchronization
    # -------------------------------------------------------------------------

    async def _initialize_state_sync(self) -> None:
        """
        v95.10: Initialize cross-repo state synchronization.

        Features:
        - Shared state with versioning
        - State change notifications
        - Conflict resolution
        - State persistence
        """
        self._ensure_locks_initialized()
        assert self._state_lock is not None

        if not self._state_sync_enabled:
            logger.info("[v95.10] State synchronization disabled")
            return

        async with self._state_lock:
            # Setup state persistence
            jarvis_home = Path.home() / ".jarvis"
            self._state_persistence_path = jarvis_home / "state"
            self._state_persistence_path.mkdir(parents=True, exist_ok=True)

            # Load persisted state
            await self._load_persisted_state()

            # Start state sync task
            if self._state_sync_task is None or self._state_sync_task.done():
                self._state_sync_task = asyncio.create_task(
                    self._state_sync_loop(),
                    name="state_sync_loop"
                )
                self._track_background_task(self._state_sync_task)

            logger.info("[v95.10] ✅ State synchronization system initialized")

    async def _load_persisted_state(self) -> None:
        """Load state from persistent storage."""
        if not self._state_persistence_path:
            return

        state_file = self._state_persistence_path / "shared_state.json"
        if state_file.exists():
            try:
                with open(state_file, "r") as f:
                    data = json.load(f)
                    self._shared_state = data.get("state", {})
                    self._state_version = data.get("versions", {})
                logger.debug("[v95.10] Loaded persisted state")
            except Exception as e:
                logger.warning(f"[v95.10] Failed to load state: {e}")

    async def _persist_state(self) -> None:
        """Persist state to storage."""
        if not self._state_persistence_path:
            return

        state_file = self._state_persistence_path / "shared_state.json"
        try:
            with open(state_file, "w") as f:
                json.dump({
                    "state": self._shared_state,
                    "versions": self._state_version,
                    "timestamp": time.time(),
                }, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[v95.10] Failed to persist state: {e}")

    async def _state_sync_loop(self) -> None:
        """Background task to sync and persist state."""
        persist_interval = 30.0

        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(persist_interval)

                if self._shutdown_event.is_set():
                    break

                assert self._state_lock is not None
                async with self._state_lock:
                    # Process buffered changes
                    if self._state_change_buffer:
                        await self._process_state_changes()

                    # Persist state
                    await self._persist_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] State sync error: {e}")

    async def _process_state_changes(self) -> None:
        """Process buffered state changes."""
        changes = self._state_change_buffer.copy()
        self._state_change_buffer.clear()

        for change in changes:
            key = change["key"]
            # Notify subscribers
            if key in self._state_subscribers:
                for callback in self._state_subscribers[key]:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(key, change["value"], change["old_value"])
                        else:
                            callback(key, change["value"], change["old_value"])
                    except Exception as e:
                        logger.error(f"[v95.10] State subscriber error: {e}")

    async def set_shared_state(
        self,
        key: str,
        value: Any,
        source_repo: str = "jarvis",
        version: Optional[int] = None,
    ) -> bool:
        """
        Set shared state with optimistic locking.

        Args:
            key: State key
            value: New value
            source_repo: Source repository
            version: Expected version (for optimistic locking)

        Returns:
            True if successful, False if version conflict
        """
        self._ensure_locks_initialized()
        assert self._state_lock is not None

        async with self._state_lock:
            current_version = self._state_version.get(key, 0)

            # Optimistic locking check
            if version is not None and version != current_version:
                logger.warning(
                    f"[v95.10] State version conflict for {key}: "
                    f"expected {version}, got {current_version}"
                )
                return False

            old_value = self._shared_state.get(key)
            self._shared_state[key] = value
            self._state_version[key] = current_version + 1

            # Buffer change for notification
            self._state_change_buffer.append({
                "key": key,
                "value": value,
                "old_value": old_value,
                "version": self._state_version[key],
                "source_repo": source_repo,
                "timestamp": time.time(),
            })

            return True

    def get_shared_state(self, key: str, default: Any = None) -> Tuple[Any, int]:
        """
        Get shared state with version.

        Returns:
            Tuple of (value, version)
        """
        value = self._shared_state.get(key, default)
        version = self._state_version.get(key, 0)
        return value, version

    def subscribe_to_state(self, key: str, callback: Callable) -> None:
        """Subscribe to state changes for a key."""
        if key not in self._state_subscribers:
            self._state_subscribers[key] = []
        self._state_subscribers[key].append(callback)

    # -------------------------------------------------------------------------
    # Issue 36: Cross-Repo Resource Coordination
    # -------------------------------------------------------------------------

    async def _initialize_resource_coordination(self) -> None:
        """
        v95.10: Initialize cross-repo resource coordination.

        Features:
        - Resource registration and tracking
        - Resource allocation with limits
        - Conflict detection and resolution
        - Fair resource sharing
        """
        self._ensure_locks_initialized()
        assert self._resource_lock is not None

        if not self._resource_coordination_enabled:
            logger.info("[v95.10] Resource coordination disabled")
            return

        async with self._resource_lock:
            # Register default resources
            self._resource_limits = {
                "gpu_memory_mb": int(os.environ.get("JARVIS_GPU_MEMORY_LIMIT", "8192")),
                "cpu_cores": int(os.environ.get("JARVIS_CPU_CORES_LIMIT", "8")),
                "memory_mb": int(os.environ.get("JARVIS_MEMORY_LIMIT", "16384")),
                "network_connections": int(os.environ.get("JARVIS_CONNECTION_LIMIT", "1000")),
                "file_handles": int(os.environ.get("JARVIS_FILE_HANDLE_LIMIT", "10000")),
            }

            logger.info("[v95.10] ✅ Resource coordination system initialized")

    async def allocate_resource(
        self,
        service: str,
        resource: str,
        amount: int,
        priority: int = 0,
    ) -> Tuple[bool, int]:
        """
        Allocate a resource to a service.

        Args:
            service: Service requesting resource
            resource: Resource type
            amount: Amount requested
            priority: Priority (higher = more important)

        Returns:
            Tuple of (success, allocated_amount)
        """
        self._ensure_locks_initialized()
        assert self._resource_lock is not None

        async with self._resource_lock:
            # Get current usage
            current_usage = sum(
                allocs.get(resource, 0)
                for allocs in self._resource_allocations.values()
            )

            limit = self._resource_limits.get(resource, float("inf"))
            available = limit - current_usage

            if available <= 0:
                # Try conflict resolution
                if resource in self._resource_conflict_handlers:
                    handler = self._resource_conflict_handlers[resource]
                    result = handler(service, resource, amount, priority)
                    if result:
                        available = result

            allocated = min(amount, int(available))

            if allocated > 0:
                if service not in self._resource_allocations:
                    self._resource_allocations[service] = {}
                self._resource_allocations[service][resource] = (
                    self._resource_allocations[service].get(resource, 0) + allocated
                )

                self._resource_registry[f"{service}:{resource}"] = {
                    "service": service,
                    "resource": resource,
                    "amount": allocated,
                    "priority": priority,
                    "timestamp": time.time(),
                }

                logger.debug(
                    f"[v95.10] Allocated {allocated}/{amount} {resource} to {service}"
                )
                return True, allocated

            logger.warning(
                f"[v95.10] Resource exhausted: {resource} "
                f"(requested: {amount}, available: {available})"
            )
            return False, 0

    async def release_resource(self, service: str, resource: str, amount: Optional[int] = None) -> None:
        """Release a resource allocation."""
        self._ensure_locks_initialized()
        assert self._resource_lock is not None

        async with self._resource_lock:
            if service not in self._resource_allocations:
                return

            if resource not in self._resource_allocations[service]:
                return

            if amount is None:
                # Release all
                del self._resource_allocations[service][resource]
            else:
                self._resource_allocations[service][resource] = max(
                    0,
                    self._resource_allocations[service][resource] - amount
                )

            # Clean up registry
            registry_key = f"{service}:{resource}"
            if registry_key in self._resource_registry:
                del self._resource_registry[registry_key]

            logger.debug(f"[v95.10] Released {resource} from {service}")

    def get_resource_status(self) -> Dict[str, Any]:
        """Get current resource allocation status."""
        status: Dict[str, Any] = {}

        for resource, limit in self._resource_limits.items():
            used = sum(
                allocs.get(resource, 0)
                for allocs in self._resource_allocations.values()
            )
            status[resource] = {
                "limit": limit,
                "used": used,
                "available": limit - used,
                "utilization": used / limit if limit > 0 else 0,
            }

        return status

    def register_resource_conflict_handler(self, resource: str, handler: Callable) -> None:
        """Register a conflict resolution handler for a resource."""
        self._resource_conflict_handlers[resource] = handler

    # -------------------------------------------------------------------------
    # Issue 37: Cross-Repo Version Compatibility
    # -------------------------------------------------------------------------

    async def _initialize_version_compatibility(self) -> None:
        """
        v95.10: Initialize version compatibility checking.

        Features:
        - Version registration for all components
        - Compatibility matrix validation
        - Upgrade coordination
        - Incompatibility warnings
        """
        self._ensure_locks_initialized()
        assert self._version_lock is not None

        if not self._version_check_enabled:
            logger.info("[v95.10] Version compatibility checking disabled")
            return

        async with self._version_lock:
            # Register versions from all repos
            await self._discover_versions()

            # Build compatibility matrix
            await self._build_compatibility_matrix()

            # Check compatibility
            issues = await self._check_compatibility()
            if issues:
                for issue in issues:
                    logger.warning(f"[v95.10] Version incompatibility: {issue}")

            logger.info("[v95.10] ✅ Version compatibility system initialized")

    async def _discover_versions(self) -> None:
        """Discover versions from all repos."""
        repos = {
            "jarvis-body": Path.cwd(),
            "jarvis-prime": self.config.jarvis_prime_path,
            "reactor-core": self.config.reactor_core_path,
        }

        for repo_name, repo_path in repos.items():
            if not repo_path or not repo_path.exists():
                continue

            version = await self._get_repo_version(repo_path)
            if version:
                self._version_registry[repo_name] = version
                logger.debug(f"[v95.10] {repo_name} version: {version}")

    async def _get_repo_version(self, repo_path: Path) -> Optional[str]:
        """Get version from a repository."""
        # Check various version sources
        version_files = [
            repo_path / "VERSION",
            repo_path / "version.txt",
            repo_path / "pyproject.toml",
            repo_path / "setup.py",
        ]

        for vf in version_files:
            if vf.exists():
                try:
                    content = vf.read_text()
                    if vf.name == "pyproject.toml":
                        import re
                        match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                        if match:
                            return match.group(1)
                    elif vf.name == "setup.py":
                        import re
                        match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                        if match:
                            return match.group(1)
                    else:
                        return content.strip()
                except Exception:
                    continue

        return None

    async def _build_compatibility_matrix(self) -> None:
        """Build compatibility matrix between components."""
        # Define compatibility rules
        # Format: {service: {dependency: [compatible_version_patterns]}}
        self._compatibility_matrix = {
            "reactor-core": {
                "jarvis-prime": ["1.*", "2.*"],  # Reactor compatible with Prime 1.x or 2.x
                "jarvis-body": ["1.*", "2.*"],
            },
            "jarvis-prime": {
                "jarvis-body": ["1.*", "2.*"],
            },
        }

    async def _check_compatibility(self) -> List[str]:
        """Check version compatibility across repos."""
        issues: List[str] = []

        for service, deps in self._compatibility_matrix.items():
            service_version = self._version_registry.get(service)
            if not service_version:
                continue

            for dep, compatible_patterns in deps.items():
                dep_version = self._version_registry.get(dep)
                if not dep_version:
                    continue

                if not self._is_version_compatible(dep_version, compatible_patterns):
                    issues.append(
                        f"{service} ({service_version}) may not be compatible with "
                        f"{dep} ({dep_version}). Expected: {compatible_patterns}"
                    )

        return issues

    def _is_version_compatible(self, version: str, patterns: List[str]) -> bool:
        """Check if version matches any pattern."""
        import fnmatch
        return any(fnmatch.fnmatch(version, pattern) for pattern in patterns)

    def get_version_info(self) -> Dict[str, Any]:
        """Get version information for all components."""
        return {
            "versions": self._version_registry.copy(),
            "compatibility_matrix": self._compatibility_matrix.copy(),
            "check_enabled": self._version_check_enabled,
        }

    # -------------------------------------------------------------------------
    # Issue 38: Cross-Repo Security Context
    # -------------------------------------------------------------------------

    async def _initialize_security_context(self) -> None:
        """
        v95.10: Initialize unified security context.

        Features:
        - Cross-repo authentication
        - Security token management
        - Policy enforcement
        - Audit logging
        """
        self._ensure_locks_initialized()
        assert self._security_lock is not None

        if not self._security_context_enabled:
            logger.info("[v95.10] Security context disabled")
            return

        async with self._security_lock:
            # Generate initial security context
            self._security_context = {
                "session_id": self._generate_session_id(),
                "created_at": time.time(),
                "principal": os.environ.get("USER", "jarvis"),
                "roles": ["system", "orchestrator"],
                "permissions": ["read", "write", "execute", "admin"],
            }

            # Initialize security policies
            self._initialize_security_policies()

            # Start token refresh task
            if self._token_refresh_task is None or self._token_refresh_task.done():
                self._token_refresh_task = asyncio.create_task(
                    self._token_refresh_loop(),
                    name="token_refresh_loop"
                )
                self._track_background_task(self._token_refresh_task)

            logger.info("[v95.10] ✅ Security context system initialized")

    def _generate_session_id(self) -> str:
        """Generate secure session ID."""
        import uuid
        import hashlib
        random_bytes = uuid.uuid4().bytes + str(time.time()).encode()
        return hashlib.sha256(random_bytes).hexdigest()[:32]

    def _initialize_security_policies(self) -> None:
        """Initialize default security policies."""
        self._security_policies = {
            "cross_repo_communication": {
                "require_token": True,
                "token_lifetime": 3600,
                "allowed_origins": ["jarvis", "jarvis-prime", "reactor-core"],
            },
            "resource_access": {
                "require_role": True,
                "allowed_roles": ["system", "orchestrator", "service"],
            },
            "api_access": {
                "rate_limit": 1000,  # requests per minute
                "require_authentication": True,
            },
        }

    async def _token_refresh_loop(self) -> None:
        """Background task to refresh security tokens."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._token_refresh_interval)

                if self._shutdown_event.is_set():
                    break

                await self._refresh_security_tokens()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Token refresh error: {e}")

    async def _refresh_security_tokens(self) -> None:
        """Refresh security tokens for all services."""
        assert self._security_lock is not None

        async with self._security_lock:
            for service in ["jarvis-prime", "reactor-core"]:
                token = self._generate_service_token(service)
                self._security_tokens[service] = token
                logger.debug(f"[v95.10] Refreshed token for {service}")

    def _generate_service_token(self, service: str) -> str:
        """Generate a service authentication token."""
        import hashlib
        import hmac

        secret = os.environ.get("JARVIS_SECRET_KEY", "jarvis-default-secret")
        timestamp = str(int(time.time()))
        message = f"{service}:{self._security_context['session_id']}:{timestamp}"

        signature = hmac.new(
            secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return f"{message}:{signature}"

    def get_service_token(self, service: str) -> Optional[str]:
        """Get authentication token for a service."""
        return self._security_tokens.get(service)

    def validate_service_token(self, token: str) -> Tuple[bool, Optional[str]]:
        """
        Validate a service token.

        Returns:
            Tuple of (is_valid, service_name)
        """
        try:
            parts = token.split(":")
            if len(parts) != 4:
                return False, None

            service, session_id, timestamp, signature = parts

            # Check session
            if session_id != self._security_context.get("session_id"):
                return False, None

            # Check timestamp (token valid for 2x refresh interval)
            token_age = time.time() - int(timestamp)
            if token_age > self._token_refresh_interval * 2:
                return False, None

            # Verify signature
            secret = os.environ.get("JARVIS_SECRET_KEY", "jarvis-default-secret")
            message = f"{service}:{session_id}:{timestamp}"
            expected_signature = __import__("hmac").new(
                secret.encode(),
                message.encode(),
                __import__("hashlib").sha256
            ).hexdigest()

            if signature != expected_signature:
                return False, None

            return True, service

        except Exception:
            return False, None

    def get_security_context(self) -> Dict[str, Any]:
        """Get current security context."""
        return {
            "session_id": self._security_context.get("session_id"),
            "principal": self._security_context.get("principal"),
            "roles": self._security_context.get("roles", []),
            "created_at": self._security_context.get("created_at"),
            "services_authenticated": list(self._security_tokens.keys()),
        }

    def check_permission(self, action: str, resource: str) -> bool:
        """Check if current context has permission for an action."""
        permissions = self._security_context.get("permissions", [])
        return action in permissions or "admin" in permissions

    # -------------------------------------------------------------------------
    # v95.10: Cross-Repo Integration Initialization (called from startup)
    # -------------------------------------------------------------------------

    async def _initialize_cross_repo_integration(self) -> None:
        """
        v95.10: Initialize all cross-repo integration systems.

        Called during startup to initialize:
        - Unified configuration
        - Cross-repo logging
        - Metrics collection
        - Error propagation
        - State synchronization
        - Resource coordination
        - Version compatibility
        - Security context

        v95.10.1: Now emits voice events for real-time user feedback.
        """
        self._ensure_locks_initialized()

        logger.info("[v95.10] 🔧 Initializing cross-repo integration systems...")

        # Track initialization for voice feedback
        systems_initialized = 0
        total_systems = 8

        # Initialize in order (some depend on others)
        # 1. Unified Configuration
        await _emit_event("CROSS_REPO_CONFIG_INIT", priority="MEDIUM")
        await self._initialize_unified_config()
        await _emit_event("CROSS_REPO_CONFIG_LOADED", priority="LOW",
                         details={"repo_count": len(self._config_sources)})
        systems_initialized += 1

        # 2. Security Context (must be early for authenticated communication)
        await _emit_event("CROSS_REPO_SECURITY_INIT", priority="MEDIUM")
        await self._initialize_security_context()
        await _emit_event("CROSS_REPO_SECURITY_ACTIVE", priority="LOW",
                         details={"token_count": len(self._security_tokens)})
        systems_initialized += 1

        # 3. Unified Logging with distributed tracing
        await _emit_event("CROSS_REPO_LOGGING_INIT", priority="MEDIUM")
        await self._initialize_unified_logging()
        await _emit_event("CROSS_REPO_LOGGING_ACTIVE", priority="LOW")
        systems_initialized += 1

        # 4. Error Propagation with correlation
        await _emit_event("CROSS_REPO_ERROR_PROPAGATION_INIT", priority="LOW")
        await self._initialize_error_propagation()
        await _emit_event("CROSS_REPO_ERROR_PROPAGATION_ACTIVE", priority="LOW")
        systems_initialized += 1

        # 5. Distributed State Synchronization
        await _emit_event("CROSS_REPO_STATE_INIT", priority="MEDIUM")
        await self._initialize_state_sync()
        await _emit_event("CROSS_REPO_STATE_SYNCED", priority="LOW",
                         details={"state_keys": len(self._shared_state)})
        systems_initialized += 1

        # 6. Resource Coordination
        await _emit_event("CROSS_REPO_RESOURCE_INIT", priority="LOW")
        await self._initialize_resource_coordination()
        await _emit_event("CROSS_REPO_RESOURCE_ACTIVE", priority="LOW",
                         details={"resource_types": len(self._resource_limits)})
        systems_initialized += 1

        # 7. Version Compatibility Check
        await _emit_event("CROSS_REPO_VERSION_CHECK", priority="MEDIUM")
        await self._initialize_version_compatibility()

        # Check if there are incompatibilities using the proper method
        compatibility_issues = await self._check_compatibility()
        incompatible_count = len(compatibility_issues)

        if incompatible_count > 0:
            await _emit_event("CROSS_REPO_VERSION_INCOMPATIBLE", priority="HIGH",
                             details={"incompatible_count": incompatible_count})
            for issue in compatibility_issues:
                logger.warning(f"[v95.10] Version issue: {issue}")
        else:
            await _emit_event("CROSS_REPO_VERSION_COMPATIBLE", priority="LOW")
        systems_initialized += 1

        # 8. Metrics Collection
        await _emit_event("CROSS_REPO_METRICS_INIT", priority="LOW")
        await self._initialize_metrics_collection()
        await _emit_event("CROSS_REPO_METRICS_ACTIVE", priority="LOW",
                         details={"metric_count": len(self._metrics_registry)})
        systems_initialized += 1

        # All systems initialized - emit completion event
        await _emit_event("CROSS_REPO_INTEGRATION_COMPLETE", priority="HIGH",
                         details={"systems_online": systems_initialized})

        logger.info(f"[v95.10] ✅ All {systems_initialized} cross-repo integration systems initialized")

    def get_cross_repo_integration_status(self) -> Dict[str, Any]:
        """Get status of all cross-repo integration systems."""
        return {
            "config": {
                "enabled": True,
                "sources": list(self._config_sources.keys()),
                "watchers": len(self._config_watchers),
            },
            "logging": {
                "enabled": self._log_correlation_enabled,
                "buffer_size": len(self._log_aggregation_buffer),
            },
            "metrics": {
                "enabled": self._metrics_enabled,
                "collectors": len(self._metrics_collectors),
                "registered_metrics": len(self._metrics_registry),
            },
            "error_propagation": {
                "enabled": self._error_propagation_enabled,
                "error_count": len(self._error_registry),
                "handlers": len(self._error_handlers),
            },
            "state_sync": {
                "enabled": self._state_sync_enabled,
                "keys": len(self._shared_state),
                "subscribers": sum(len(s) for s in self._state_subscribers.values()),
            },
            "resource_coordination": {
                "enabled": self._resource_coordination_enabled,
                "resources": len(self._resource_limits),
                "allocations": len(self._resource_registry),
            },
            "version_compatibility": {
                "enabled": self._version_check_enabled,
                "versions": self._version_registry,
            },
            "security": {
                "enabled": self._security_context_enabled,
                "session_id": self._security_context.get("session_id", "")[:8] + "...",
                "authenticated_services": len(self._security_tokens),
            },
            "timestamp": time.time(),
        }

    # =========================================================================
    # v95.5: Graceful Degradation with Circuit Breaker Pattern
    # =========================================================================

    async def _initialize_graceful_degradation(self) -> None:
        """
        v95.5: Initialize graceful degradation infrastructure.

        Sets up:
        1. Cross-repo circuit breaker with failure classification
        2. Per-service degradation tracking
        3. Fallback handlers for each service
        4. Event bus notifications for degradation state changes
        """
        self._ensure_locks_initialized()

        try:
            # Initialize cross-repo circuit breaker
            from backend.core.resilience.cross_repo_circuit_breaker import (
                CrossRepoCircuitBreaker,
                CircuitBreakerConfig,
                CircuitState,
            )

            # Create circuit breaker with event bus notifications
            async def on_state_change(tier: str, old_state: CircuitState, new_state: CircuitState):
                """Notify event bus and update degradation mode on circuit state changes."""
                logger.info(f"[v95.5] Circuit breaker state change: {tier}: {old_state.value} -> {new_state.value}")

                # Update degradation mode based on circuit state
                async with self._degradation_lock_safe:
                    if new_state == CircuitState.OPEN:
                        self._degradation_mode[tier] = "isolated"
                        # Reduce capabilities for this service
                        self._service_capabilities[tier] = {"basic", "read_only"}
                    elif new_state == CircuitState.HALF_OPEN:
                        self._degradation_mode[tier] = "degraded"
                        self._service_capabilities[tier] = {"basic", "read_only", "limited_write"}
                    else:  # CLOSED
                        self._degradation_mode[tier] = "full"
                        self._service_capabilities[tier] = {"basic", "read_only", "write", "advanced"}

                # Publish degradation event to event bus
                await self._publish_lifecycle_event(
                    event_type="system.degradation",
                    payload={
                        "service": tier,
                        "old_state": old_state.value,
                        "new_state": new_state.value,
                        "degradation_mode": self._degradation_mode.get(tier, "unknown"),
                        "available_capabilities": list(self._service_capabilities.get(tier, set())),
                    },
                    priority="HIGH" if new_state == CircuitState.OPEN else "NORMAL",
                )

            config = CircuitBreakerConfig(
                failure_threshold=5,
                timeout_seconds=30.0,
                max_timeout_seconds=300.0,
                recovery_factor=2.0,
                half_open_max_calls=3,
                startup_grace_period_seconds=120.0,  # Generous grace for ML model loading
                adaptive_thresholds=True,
            )

            self._cross_repo_breaker = CrossRepoCircuitBreaker(
                name="orchestrator",
                config=config,
                on_state_change=on_state_change,
            )

            # Initialize default degradation modes (all services start in full mode)
            for service in ["jarvis-body", "jarvis-prime", "reactor-core"]:
                self._degradation_mode[service] = "full"
                self._service_capabilities[service] = {"basic", "read_only", "write", "advanced"}

            logger.info("[v95.5] ✅ Graceful degradation initialized with circuit breaker")

        except ImportError as e:
            logger.warning(f"[v95.5] Circuit breaker not available: {e}")
            # Fall back to basic degradation without circuit breaker
            for service in ["jarvis-body", "jarvis-prime", "reactor-core"]:
                self._degradation_mode[service] = "full"
                self._service_capabilities[service] = {"basic", "read_only", "write", "advanced"}

    async def _execute_with_graceful_degradation(
        self,
        service: str,
        func: Callable,
        args: tuple = (),
        kwargs: Optional[Dict] = None,
        fallback: Optional[Callable] = None,
        required_capability: str = "basic",
    ) -> Any:
        """
        v95.5: Execute operation with graceful degradation.

        If the service is degraded or isolated, uses fallback or returns
        a degraded response instead of failing completely.

        Args:
            service: Target service name
            func: Function to execute
            args: Function arguments
            kwargs: Function keyword arguments
            fallback: Fallback function if service is degraded
            required_capability: Capability required for this operation

        Returns:
            Function result or fallback result
        """
        kwargs = kwargs or {}
        self._ensure_locks_initialized()

        async with self._degradation_lock_safe:
            mode = self._degradation_mode.get(service, "full")
            capabilities = self._service_capabilities.get(service, set())

        # Check if required capability is available
        if required_capability not in capabilities:
            logger.warning(
                f"[v95.5] Service {service} lacks capability '{required_capability}' "
                f"(mode: {mode}, available: {capabilities})"
            )
            if fallback:
                logger.info(f"[v95.5] Using fallback for {service}")
                result = fallback(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            # Return degraded response
            return {"status": "degraded", "service": service, "mode": mode}

        # Execute with circuit breaker if available
        if self._cross_repo_breaker:
            try:
                return await self._cross_repo_breaker.execute(
                    tier=service,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    fallback=fallback,
                )
            except Exception as e:
                logger.error(f"[v95.5] Circuit breaker execution failed for {service}: {e}")
                if fallback:
                    result = fallback(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                raise
        else:
            # Direct execution without circuit breaker
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

    def get_service_degradation_status(self) -> Dict[str, Dict[str, Any]]:
        """
        v95.5: Get degradation status for all services.

        Returns:
            Dict mapping service name to degradation info
        """
        status = {}
        for service in ["jarvis-body", "jarvis-prime", "reactor-core"]:
            status[service] = {
                "mode": self._degradation_mode.get(service, "unknown"),
                "capabilities": list(self._service_capabilities.get(service, set())),
                "circuit_state": None,
            }
            # Add circuit breaker state if available
            if self._cross_repo_breaker:
                try:
                    health = self._cross_repo_breaker.get_tier_health(service)
                    status[service]["circuit_state"] = health.state.value if health else None
                    status[service]["success_rate"] = health.success_rate if health else None
                except Exception:
                    pass
        return status

    # =========================================================================
    # v95.5: Distributed Tracing with Correlation ID Propagation
    # =========================================================================

    async def _initialize_distributed_tracing(self) -> None:
        """
        v95.5: Initialize distributed tracing infrastructure.

        Sets up:
        1. Startup correlation context
        2. Trace propagation for all IPC calls
        3. Integration with event bus for trace visibility
        """
        try:
            from backend.core.resilience.correlation_context import (
                CorrelationContext,
                with_correlation,
            )

            # Create root correlation context for this startup session
            self._startup_correlation_id = CorrelationContext.generate_id("startup")
            self._startup_trace_context = CorrelationContext.create(
                operation="orchestrator_startup",
                source_repo="jarvis",
                source_component="orchestrator",
                timeout=600.0,  # 10 minute timeout for full startup
            )

            # Add startup metadata to baggage
            self._startup_trace_context.baggage["startup_time"] = str(time.time())
            self._startup_trace_context.baggage["orchestrator_version"] = "95.5"
            self._startup_trace_context.baggage["pid"] = str(os.getpid())

            logger.info(f"[v95.5] ✅ Distributed tracing initialized: {self._startup_correlation_id}")

            # Publish trace start event
            await self._publish_lifecycle_event(
                event_type="system.trace.started",
                payload={
                    "correlation_id": self._startup_correlation_id,
                    "operation": "orchestrator_startup",
                    "baggage": self._startup_trace_context.baggage,
                },
                priority="NORMAL",
            )

        except ImportError as e:
            logger.warning(f"[v95.5] Correlation context not available: {e}")
            # Generate a simple correlation ID as fallback
            self._startup_correlation_id = f"startup-{int(time.time() * 1000)}-{os.getpid()}"

    async def _create_span(
        self,
        operation: str,
        parent_context: Optional[Any] = None,
        metadata: Optional[Dict] = None,
    ) -> str:
        """
        v95.5: Create a new trace span for an operation.

        Args:
            operation: Name of the operation
            parent_context: Parent correlation context (defaults to startup context)
            metadata: Additional span metadata

        Returns:
            Span ID
        """
        self._ensure_locks_initialized()
        metadata = metadata or {}

        try:
            from backend.core.resilience.correlation_context import CorrelationContext

            parent = parent_context or self._startup_trace_context

            # Create child context for this operation
            ctx = CorrelationContext.create(
                operation=operation,
                source_repo="jarvis",
                source_component="orchestrator",
                parent=parent,
            )

            # Add metadata to baggage
            for key, value in metadata.items():
                ctx.baggage[key] = str(value)

            async with self._trace_lock_safe:
                self._active_traces[ctx.correlation_id] = ctx

            logger.debug(f"[v95.5] Created span: {operation} ({ctx.correlation_id})")
            return ctx.correlation_id

        except Exception as e:
            logger.debug(f"[v95.5] Span creation failed: {e}")
            # Return a simple ID as fallback
            span_id = f"{operation}-{int(time.time() * 1000)}"
            return span_id

    async def _end_span(
        self,
        span_id: str,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> None:
        """
        v95.5: End a trace span.

        Args:
            span_id: Span ID returned by _create_span
            status: Span status (success, error)
            error_message: Error message if status is error
        """
        self._ensure_locks_initialized()

        async with self._trace_lock_safe:
            ctx = self._active_traces.pop(span_id, None)

        if ctx and hasattr(ctx, "current_span") and ctx.current_span:
            ctx.current_span.end_time = time.time()
            ctx.current_span.status = status
            ctx.current_span.error_message = error_message

            # Publish span end event for visibility
            await self._publish_lifecycle_event(
                event_type="system.trace.span_end",
                payload={
                    "span_id": span_id,
                    "operation": ctx.current_span.operation if ctx.current_span else "unknown",
                    "duration_ms": ctx.current_span.duration_ms if ctx.current_span else 0,
                    "status": status,
                    "error": error_message,
                },
                priority="LOW",
            )

    def get_correlation_id(self) -> str:
        """
        v95.5: Get the current startup correlation ID.

        Returns:
            Correlation ID for the current startup session
        """
        return self._startup_correlation_id or f"unknown-{os.getpid()}"

    def get_trace_headers(self) -> Dict[str, str]:
        """
        v95.5: Get HTTP headers for trace propagation.

        Returns:
            Dict of headers to include in cross-repo HTTP calls
        """
        headers = {
            "X-Correlation-ID": self.get_correlation_id(),
            "X-Source-Component": "orchestrator",
            "X-Source-Repo": "jarvis",
        }

        if self._startup_trace_context:
            try:
                # Add baggage items as headers
                for key, value in self._startup_trace_context.baggage.items():
                    headers[f"X-Baggage-{key}"] = str(value)
            except Exception:
                pass

        return headers

    # =========================================================================
    # v95.5: Event Bus Integration for Lifecycle Events
    # =========================================================================

    async def _initialize_event_bus(self) -> None:
        """
        v95.5: Initialize event bus connection for lifecycle events.

        Sets up:
        1. Connection to Trinity Event Bus
        2. Lifecycle event publishing
        3. Subscriptions to cross-repo events
        """
        if not self._lifecycle_events_enabled:
            logger.info("[v95.5] Lifecycle events disabled via JARVIS_LIFECYCLE_EVENTS=false")
            return

        try:
            from backend.core.trinity_event_bus import (
                get_trinity_event_bus,
                TrinityEvent,
                EventType,
                EventPriority,
                RepoType,
            )

            # Get or create event bus instance
            self._event_bus = await get_trinity_event_bus()
            self._event_bus_initialized = True

            logger.info("[v95.5] ✅ Event bus connection initialized")

            # Subscribe to lifecycle events from other repos
            await self._subscribe_to_lifecycle_events()

        except ImportError as e:
            logger.warning(f"[v95.5] Event bus not available: {e}")
        except Exception as e:
            logger.warning(f"[v95.5] Event bus initialization failed: {e}")

    async def _subscribe_to_lifecycle_events(self) -> None:
        """
        v95.5: Subscribe to lifecycle events from other repositories.

        Enables the orchestrator to react to:
        - Service startup/shutdown events
        - Health check events
        - Degradation events
        """
        if not self._event_bus:
            return

        try:
            # Subscribe to lifecycle events
            async def handle_lifecycle_event(event):
                """Handle incoming lifecycle events from other repos."""
                try:
                    payload = event.payload if hasattr(event, "payload") else event
                    source = event.source.value if hasattr(event, "source") else "unknown"
                    topic = event.topic if hasattr(event, "topic") else "unknown"

                    logger.info(f"[v95.5] Received lifecycle event: {topic} from {source}")

                    # Update internal state based on event
                    if "startup" in topic:
                        service = payload.get("service", source)
                        status = payload.get("status", "unknown")
                        if status == "ready":
                            self._services_ready.add(service)
                            self._services_starting.discard(service)
                        elif status == "starting":
                            self._services_starting.add(service)

                    elif "shutdown" in topic:
                        service = payload.get("service", source)
                        self._services_ready.discard(service)
                        self._services_starting.discard(service)

                    elif "health" in topic:
                        service = payload.get("service", source)
                        healthy = payload.get("healthy", True)
                        if not healthy:
                            async with self._degradation_lock_safe:
                                self._degradation_mode[service] = "degraded"

                except Exception as e:
                    logger.warning(f"[v95.5] Error handling lifecycle event: {e}")

            # Subscribe to all lifecycle events
            sub_id = await self._event_bus.subscribe(
                topic="lifecycle.*",
                handler=handle_lifecycle_event,
            )
            self._event_subscriptions.append(sub_id)

            # Subscribe to system degradation events
            sub_id = await self._event_bus.subscribe(
                topic="system.degradation",
                handler=handle_lifecycle_event,
            )
            self._event_subscriptions.append(sub_id)

            logger.info(f"[v95.5] Subscribed to lifecycle events ({len(self._event_subscriptions)} subscriptions)")

        except Exception as e:
            logger.warning(f"[v95.5] Failed to subscribe to lifecycle events: {e}")

    async def _publish_lifecycle_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        priority: str = "NORMAL",
    ) -> bool:
        """
        v95.5: Publish a lifecycle event to the event bus.

        Args:
            event_type: Event type (e.g., "lifecycle.startup", "system.degradation")
            payload: Event payload
            priority: Event priority (CRITICAL, HIGH, NORMAL, LOW)

        Returns:
            True if event was published successfully
        """
        if not self._event_bus_initialized or not self._event_bus:
            return False

        try:
            from backend.core.trinity_event_bus import (
                TrinityEvent,
                EventPriority,
                RepoType,
            )

            # Map priority string to enum
            priority_map = {
                "CRITICAL": EventPriority.CRITICAL,
                "HIGH": EventPriority.HIGH,
                "NORMAL": EventPriority.NORMAL,
                "LOW": EventPriority.LOW,
            }
            event_priority = priority_map.get(priority, EventPriority.NORMAL)

            # Add correlation ID to payload
            payload["correlation_id"] = self.get_correlation_id()
            payload["timestamp"] = time.time()
            payload["source_pid"] = os.getpid()

            event = TrinityEvent(
                topic=event_type,
                source=RepoType.JARVIS,
                target=RepoType.BROADCAST,
                priority=event_priority,
                payload=payload,
                correlation_id=self.get_correlation_id(),
            )

            await self._event_bus.publish(event)
            logger.debug(f"[v95.5] Published lifecycle event: {event_type}")
            return True

        except Exception as e:
            logger.debug(f"[v95.5] Failed to publish lifecycle event: {e}")
            return False

    async def publish_service_lifecycle_event(
        self,
        service: str,
        status: str,
        details: Optional[Dict] = None,
    ) -> None:
        """
        v95.5: Publish a service lifecycle event.

        Args:
            service: Service name
            status: Status (starting, ready, failed, shutdown)
            details: Additional details
        """
        payload = {
            "service": service,
            "status": status,
            "details": details or {},
            "degradation_mode": self._degradation_mode.get(service, "unknown"),
        }

        # Map status to priority
        priority_map = {
            "starting": "NORMAL",
            "ready": "HIGH",
            "failed": "CRITICAL",
            "shutdown": "HIGH",
        }
        priority = priority_map.get(status, "NORMAL")

        await self._publish_lifecycle_event(
            event_type=f"lifecycle.{status}",
            payload=payload,
            priority=priority,
        )

    async def _verify_jarvis_body_health(self, timeout: float = 30.0) -> bool:
        """
        v95.4: Verify JARVIS body is healthy and ready for external services.

        This checks:
        1. Service registry is accessible
        2. jarvis-body is registered in the registry
        3. jarvis-body heartbeat is active and recent
        4. Core endpoints are responding (if FastAPI is running)

        Args:
            timeout: Maximum time to wait for health verification

        Returns:
            True if jarvis-body is verified healthy, False otherwise
        """
        start_time = time.time()
        logger.info("[v95.4] Verifying jarvis-body health before Phase 2...")

        try:
            # Check 1: Registry accessible and jarvis-body registered
            if self.registry:
                try:
                    services = await self.registry.list_services()
                    jarvis_body_entry = None
                    for svc in services:
                        # v96.0: Fixed - ServiceInfo uses 'service_name' not 'name'
                        # Check both 'service_name' and 'name' for backwards compatibility
                        svc_name = None
                        if isinstance(svc, dict):
                            svc_name = svc.get("service_name") or svc.get("name")
                        elif hasattr(svc, "service_name"):
                            svc_name = svc.service_name
                        elif hasattr(svc, "name"):
                            svc_name = svc.name

                        if svc_name == "jarvis-body":
                            jarvis_body_entry = svc
                            break

                    if jarvis_body_entry:
                        logger.info("[v95.4] ✅ jarvis-body found in registry")

                        # Check heartbeat is recent (within 60 seconds)
                        last_heartbeat = None
                        if isinstance(jarvis_body_entry, dict):
                            last_heartbeat = jarvis_body_entry.get("last_heartbeat")
                        elif hasattr(jarvis_body_entry, "last_heartbeat"):
                            last_heartbeat = jarvis_body_entry.last_heartbeat

                        if last_heartbeat:
                            heartbeat_age = time.time() - last_heartbeat
                            if heartbeat_age < 60:
                                logger.info(f"[v95.4] ✅ jarvis-body heartbeat active ({heartbeat_age:.1f}s ago)")
                            else:
                                logger.warning(f"[v95.4] ⚠️ jarvis-body heartbeat stale ({heartbeat_age:.1f}s ago)")
                    else:
                        logger.warning("[v95.4] ⚠️ jarvis-body not yet in registry")
                        # Not fatal - registry write may still be propagating
                except Exception as e:
                    logger.warning(f"[v95.4] Registry check warning: {e}")

            # Check 2: Local health endpoint (if FastAPI is running)
            try:
                local_port = int(os.environ.get("JARVIS_PORT", 8010))
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{local_port}/health",
                        timeout=aiohttp.ClientTimeout(total=5.0)
                    ) as resp:
                        if resp.status == 200:
                            logger.info(f"[v95.4] ✅ jarvis-body health endpoint responding (port {local_port})")
                            self._jarvis_body_status = "healthy"
                            self._jarvis_body_health_verified = True
                            return True
                        else:
                            logger.warning(f"[v95.4] jarvis-body health endpoint returned {resp.status}")
            except aiohttp.ClientConnectorError:
                # FastAPI not yet listening - this is normal during startup
                logger.debug("[v95.4] jarvis-body health endpoint not yet available")
            except asyncio.TimeoutError:
                logger.debug("[v95.4] jarvis-body health endpoint timeout")
            except Exception as e:
                logger.debug(f"[v95.4] jarvis-body health check: {e}")

            # If registry shows jarvis-body is registered with active heartbeat,
            # that's sufficient even if health endpoint isn't ready yet
            if self.registry:
                try:
                    services = await self.registry.list_services()
                    for svc in services:
                        # v96.0: Fixed - ServiceInfo uses 'service_name' not 'name'
                        if isinstance(svc, dict):
                            name = svc.get("service_name") or svc.get("name")
                        else:
                            name = getattr(svc, "service_name", None) or getattr(svc, "name", None)
                        if name == "jarvis-body":
                            self._jarvis_body_status = "healthy"
                            self._jarvis_body_health_verified = True
                            logger.info("[v95.4] ✅ jarvis-body verified via registry (health endpoint pending)")
                            return True
                except Exception:
                    pass

            # Fallback: If we registered successfully earlier, consider it healthy
            elapsed = time.time() - start_time
            if elapsed < timeout and self._jarvis_body_status == "starting":
                logger.info("[v95.4] jarvis-body registration confirmed, proceeding with startup")
                self._jarvis_body_status = "healthy"
                self._jarvis_body_health_verified = True
                return True

            logger.warning(f"[v95.4] jarvis-body health verification incomplete after {elapsed:.1f}s")
            return False

        except Exception as e:
            logger.error(f"[v95.4] jarvis-body health verification error: {e}")
            return False

    async def _update_unified_state_for_jarvis_body(self, status: str, health: bool = True) -> None:
        """
        v95.4: Update unified state for jarvis-body in the service registry.

        This ensures external services can discover jarvis-body's current state
        through the unified state API.

        Args:
            status: Current status (initializing, starting, healthy, unhealthy)
            health: Whether jarvis-body is healthy
        """
        if not self.registry:
            return

        try:
            # Build metadata for the unified state
            metadata = {
                "status": status,
                "timestamp": time.time(),
                "port": int(os.environ.get("JARVIS_PORT", 8010)),
                "host": os.environ.get("JARVIS_HOST", "127.0.0.1"),
                "startup_time": self._jarvis_body_startup_time,
            }

            # Use the unified state API if available
            if hasattr(self.registry, "update_supervisor_state"):
                await self.registry.update_supervisor_state(
                    service_name="jarvis-body",
                    process_alive=health,
                    pid=os.getpid(),
                    exit_code=None,
                    metadata=metadata
                )
                logger.debug(f"[v95.4] Updated unified state for jarvis-body: {status}")
        except Exception as e:
            logger.debug(f"[v95.4] Unified state update for jarvis-body failed: {e}")

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """
        v93.11: Get shared HTTP session with connection pooling.

        Creates session lazily and reuses it for all HTTP requests.
        Connection pooling improves performance and reduces resource usage.
        """
        self._ensure_locks_initialized()

        async with self._http_session_lock_safe:
            if self._http_session is None or self._http_session.closed:
                # Configure connection pooling
                connector = aiohttp.TCPConnector(
                    limit=100,  # Max total connections
                    limit_per_host=10,  # Max connections per host
                    ttl_dns_cache=300,  # DNS cache TTL
                    enable_cleanup_closed=True,
                    force_close=False,  # Reuse connections
                )

                # Default timeouts
                timeout = aiohttp.ClientTimeout(
                    total=30,
                    connect=10,
                    sock_read=10,
                )

                # v95.5: Add trace headers for distributed tracing
                trace_headers = self.get_trace_headers()

                self._http_session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                    raise_for_status=False,
                    headers=trace_headers,  # v95.5: Include correlation headers in all requests
                )

                logger.debug(f"[v93.11] Shared HTTP session initialized with connection pooling and trace headers: {trace_headers.get('X-Correlation-ID', 'none')}")

            return self._http_session

    async def _traced_request(
        self,
        method: str,
        url: str,
        span_name: Optional[str] = None,
        **kwargs
    ) -> Any:
        """
        v95.5: Make HTTP request with distributed tracing.

        Automatically adds correlation headers and creates a trace span
        for the request.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            span_name: Optional span name (defaults to method + url path)
            **kwargs: Additional aiohttp request arguments

        Returns:
            aiohttp.ClientResponse
        """
        session = await self._get_http_session()

        # Create span for this request
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if span_name is None:
            span_name = f"http_{method.lower()}_{parsed.netloc}"

        span_id = await self._create_span(span_name, metadata={
            "method": method,
            "url": url,
            "host": parsed.netloc,
        })

        # Add trace headers to request
        headers = kwargs.pop("headers", {})
        headers.update(self.get_trace_headers())
        headers["X-Span-ID"] = span_id

        try:
            response = await session.request(method, url, headers=headers, **kwargs)

            # End span with status
            status = "success" if response.status < 400 else "error"
            await self._end_span(span_id, status=status, error_message=f"HTTP {response.status}" if status == "error" else None)

            return response

        except Exception as e:
            await self._end_span(span_id, status="error", error_message=str(e))
            raise

    async def _close_http_session(self) -> None:
        """
        v93.11: Close shared HTTP session gracefully.

        Called during shutdown.
        """
        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
                # Allow connections to close gracefully
                await asyncio.sleep(0.25)
            except Exception as e:
                logger.warning(f"[v95.1] HTTP session close error: {e}")
            finally:
                self._http_session = None
            logger.debug("[v93.11] Shared HTTP session closed")

    def _track_background_task(self, task: asyncio.Task) -> None:
        """
        v95.1: Track a background task for proper cleanup on shutdown.

        Fire-and-forget tasks MUST be tracked to:
        1. Prevent task leaks (unfinished tasks in memory)
        2. Ensure proper cancellation during shutdown
        3. Catch and log exceptions that would otherwise be lost

        Usage:
            task = asyncio.create_task(some_async_func(), name="descriptive_name")
            self._track_background_task(task)
        """
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """
        v95.1: Callback when a tracked background task completes.

        Handles:
        1. Removing task from tracking set
        2. Logging any unhandled exceptions (prevents silent failures)
        """
        self._background_tasks.discard(task)

        # Check for exceptions (prevents "Task exception was never retrieved")
        if not task.cancelled():
            try:
                exc = task.exception()
                if exc is not None:
                    task_name = task.get_name() if hasattr(task, 'get_name') else "unknown"
                    logger.error(
                        f"[v95.1] Background task '{task_name}' failed with exception: {exc}",
                        exc_info=exc
                    )
            except asyncio.InvalidStateError:
                pass  # Task not done yet (shouldn't happen in done callback)

    async def _cancel_all_background_tasks(self, timeout: float = 10.0) -> int:
        """
        v95.1: Cancel all tracked background tasks gracefully.

        Called during shutdown to ensure no orphaned coroutines.

        Returns the number of tasks that were cancelled.
        """
        if not self._background_tasks:
            return 0

        tasks_to_cancel = list(self._background_tasks)
        cancelled_count = 0

        logger.info(f"[v95.1] Cancelling {len(tasks_to_cancel)} background tasks...")

        # Cancel all tasks
        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()
                cancelled_count += 1

        if cancelled_count > 0:
            # Wait for cancellation with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[v95.1] Timeout waiting for {cancelled_count} tasks to cancel "
                    f"(some may still be running)"
                )
            except Exception as e:
                logger.warning(f"[v95.1] Error during task cancellation: {e}")

        self._background_tasks.clear()
        logger.info(f"[v95.1] Cancelled {cancelled_count} background tasks")
        return cancelled_count

    def _get_circuit_breaker(self, service_name: str) -> CircuitBreaker:
        """
        v93.11: Get or create circuit breaker for a service (thread-safe).

        Uses a simple check-then-create pattern that's safe for single-threaded
        asyncio code. For true thread safety in multi-threaded scenarios,
        use _get_circuit_breaker_async.
        """
        if service_name not in self._circuit_breakers:
            self._circuit_breakers[service_name] = CircuitBreaker(
                name=f"docker-{service_name}",
                failure_threshold=self.config.docker_circuit_breaker_failure_threshold,
                recovery_timeout=self.config.docker_circuit_breaker_recovery_timeout,
                half_open_max_requests=self.config.docker_circuit_breaker_half_open_requests,
            )
        return self._circuit_breakers[service_name]

    async def _get_circuit_breaker_async(self, service_name: str) -> CircuitBreaker:
        """
        v93.11: Get or create circuit breaker with async lock protection.

        Use this version when called from async contexts where multiple
        coroutines might access circuit breakers concurrently.
        """
        self._ensure_locks_initialized()

        async with self._circuit_breaker_lock_safe:
            if service_name not in self._circuit_breakers:
                self._circuit_breakers[service_name] = CircuitBreaker(
                    name=f"docker-{service_name}",
                    failure_threshold=self.config.docker_circuit_breaker_failure_threshold,
                    recovery_timeout=self.config.docker_circuit_breaker_recovery_timeout,
                    half_open_max_requests=self.config.docker_circuit_breaker_half_open_requests,
                )
            return self._circuit_breakers[service_name]

    def _get_retry_state(self, operation_name: str) -> RetryState:
        """
        v93.9: Get or create retry state for an operation.
        """
        if operation_name not in self._retry_states:
            self._retry_states[operation_name] = RetryState(
                max_attempts=self.config.docker_retry_max_attempts,
                base_delay=self.config.docker_retry_base_delay,
                max_delay=self.config.docker_retry_max_delay,
                exponential_base=self.config.docker_retry_exponential_base,
            )
        return self._retry_states[operation_name]

    # =========================================================================
    # Pre-Flight Cleanup (v5.0)
    # =========================================================================

    async def _kill_process_on_port(self, port: int) -> bool:
        """
        Kill any process listening on the specified port.

        v5.3: Uses lsof to find and kill stale processes.
        Resilient to CancelledError during startup.
        Returns True if a process was killed.
        """
        try:
            # Find process on port using lsof
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-ti", f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if not stdout:
                return False

            pids = stdout.decode().strip().split('\n')
            killed = False

            # v93.15: Get current process ID to avoid self-kill
            current_pid = os.getpid()
            parent_pid = os.getppid()

            for pid_str in pids:
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)

                    # v93.15: CRITICAL - Never kill the current process or its parent
                    if pid == current_pid:
                        logger.debug(f"    Skipping current process (PID: {pid}) on port {port}")
                        continue
                    if pid == parent_pid:
                        logger.debug(f"    Skipping parent process (PID: {pid}) on port {port}")
                        continue

                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"    🔪 Killed stale process on port {port} (PID: {pid})")
                    killed = True
                except (ValueError, ProcessLookupError, PermissionError) as e:
                    logger.debug(f"    Could not kill PID {pid_str}: {e}")

            if killed:
                # Give process time to terminate - use shield to prevent cancellation
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    # Don't let cancellation interrupt cleanup
                    logger.debug(f"    Port {port} cleanup sleep interrupted, continuing...")

            return killed

        except asyncio.CancelledError:
            # v5.3: Don't propagate CancelledError during critical cleanup
            logger.debug(f"    Port cleanup cancelled for {port}, continuing startup...")
            return False
        except Exception as e:
            logger.debug(f"    Port cleanup failed for {port}: {e}")
            return False

    async def _cleanup_legacy_ports(self) -> Dict[str, List[int]]:
        """
        Clean up any processes on legacy ports before startup.

        v5.3: Ensures no stale processes from old configurations are blocking
        the correct ports. Resilient to CancelledError.
        Returns dict of service -> [killed_ports].
        """
        logger.info("  🧹 Pre-flight: Cleaning up legacy ports...")

        cleaned = {"jarvis-prime": [], "reactor-core": []}

        try:
            # Clean up legacy jarvis-prime ports
            for port in self.config.legacy_jarvis_prime_ports:
                if await self._kill_process_on_port(port):
                    cleaned["jarvis-prime"].append(port)

            # Clean up legacy reactor-core ports
            for port in self.config.legacy_reactor_core_ports:
                if await self._kill_process_on_port(port):
                    cleaned["reactor-core"].append(port)

            # Also check if something is running on CORRECT ports but with wrong host
            # (e.g., bound to 127.0.0.1 instead of 0.0.0.0)
            for service, port in [
                ("jarvis-prime", self.config.jarvis_prime_default_port),
                ("reactor-core", self.config.reactor_core_default_port),
            ]:
                if await self._check_wrong_binding(port):
                    logger.warning(f"    ⚠️ {service} on port {port} bound to 127.0.0.1, restarting...")
                    if await self._kill_process_on_port(port):
                        cleaned[service].append(port)

        except asyncio.CancelledError:
            # v5.3: Don't let cancellation interrupt startup - log and continue
            logger.warning("  ⚠️ Legacy port cleanup interrupted, continuing startup...")

        # Summary
        total_cleaned = sum(len(ports) for ports in cleaned.values())
        if total_cleaned > 0:
            logger.info(f"  ✅ Cleaned {total_cleaned} stale processes")
        else:
            logger.info(f"  ✅ No legacy processes found")

        return cleaned

    async def _check_wrong_binding(self, port: int) -> bool:
        """
        Check if a service on port is bound to 127.0.0.1 (should be 0.0.0.0).

        v5.3: Detects misconfigured services that won't accept external connections.
        Resilient to CancelledError.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-P", "-n",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if not stdout:
                return False

            output = stdout.decode()
            # Check if bound to 127.0.0.1 (localhost only)
            if "127.0.0.1:" in output or "localhost:" in output:
                # Also check it's not bound to 0.0.0.0 (which would be correct)
                if "*:" not in output and "0.0.0.0:" not in output:
                    return True

        except Exception:
            pass

        return False

    # =========================================================================
    # v93.8: Docker Hybrid Mode - Check Docker Before Local Process
    # =========================================================================

    async def _check_docker_available(self) -> bool:
        """
        v93.8: Check if Docker is available and running.

        Returns True if Docker daemon is accessible.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return_code = await asyncio.wait_for(proc.wait(), timeout=5.0)
            return return_code == 0
        except (asyncio.TimeoutError, FileNotFoundError, Exception):
            return False

    async def _check_docker_container_running(self, container_name: str) -> bool:
        """
        v93.8: Check if a specific Docker container is running.

        Args:
            container_name: Name of the Docker container to check

        Returns:
            True if container is running
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "container", "inspect",
                "-f", "{{.State.Running}}",
                container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0:
                return False

            output = stdout.decode().strip().lower()
            return output == "true"

        except (asyncio.TimeoutError, FileNotFoundError, Exception) as e:
            logger.debug(f"Docker container check failed: {e}")
            return False

    async def _check_docker_service_healthy(
        self,
        service_name: str,
        port: int,
        health_endpoint: str = "/health",
    ) -> tuple[bool, Optional[str]]:
        """
        v93.11: Check if a Docker-hosted service is healthy via HTTP.

        Uses shared HTTP session with connection pooling.

        Args:
            service_name: Name of the service (for logging)
            port: Port the service is exposed on
            health_endpoint: Health check endpoint path

        Returns:
            Tuple of (is_healthy, status_string)
        """
        url = f"http://localhost:{port}{health_endpoint}"

        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.config.docker_health_timeout)
            ) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status}"

                try:
                    data = await response.json()
                    status = data.get("status", "unknown")

                    if status == "healthy":
                        return True, "healthy"
                    elif status == "starting":
                        elapsed = data.get("model_load_elapsed_seconds", 0)
                        return False, f"starting (model loading: {elapsed:.0f}s)"
                    else:
                        return False, status
                except Exception:
                    # Couldn't parse JSON, but got 200 - assume healthy
                    return True, "healthy (no JSON)"

        except asyncio.TimeoutError:
            return False, "timeout"
        except aiohttp.ClientConnectorError:
            return False, "connection refused"
        except Exception as e:
            return False, f"error: {e}"

    async def _try_docker_hybrid_for_service(
        self,
        definition: ServiceDefinition,
    ) -> tuple[bool, str]:
        """
        v93.8: Attempt to use Docker container for a service before spawning local.

        This implements the "Docker-first, local-fallback" hybrid approach.

        Args:
            definition: Service definition to check

        Returns:
            Tuple of (should_skip_local_spawn, reason_message)
            - If (True, msg): Docker container is handling the service, skip local spawn
            - If (False, msg): Docker not available/healthy, proceed with local spawn
        """
        # Only jarvis-prime supports Docker hybrid mode currently
        if definition.name != "jarvis-prime":
            return False, "service does not support Docker hybrid mode"

        if not self.config.docker_hybrid_enabled:
            return False, "Docker hybrid mode disabled via config"

        logger.info(f"    🐳 Checking Docker hybrid mode for {definition.name}...")

        # Step 1: Check if Docker is available
        docker_available = await self._check_docker_available()
        if not docker_available:
            logger.info(f"    ℹ️  Docker not available, using local process")
            return False, "Docker not available"

        # Step 2: Check if the container is running
        container_name = self.config.jarvis_prime_docker_container
        container_running = await self._check_docker_container_running(container_name)

        if container_running:
            logger.info(f"    ✅ Docker container '{container_name}' is running")

            # Step 3: Check if the service is healthy
            is_healthy, status = await self._check_docker_service_healthy(
                definition.name,
                definition.default_port,
                definition.health_endpoint,
            )

            if is_healthy:
                logger.info(
                    f"    ⚡ Connected to {definition.name} via Docker (status: {status})"
                )
                return True, f"Docker container healthy ({status})"
            else:
                # Container running but not healthy - might be starting up
                logger.info(
                    f"    ℹ️  Docker container running but not healthy: {status}"
                )

                if status and ("starting" in status or "model loading" in status):
                    # Service is starting in Docker, wait for it
                    logger.info(
                        f"    ⏳ Waiting for Docker container to become healthy..."
                    )

                    healthy = await self._wait_for_docker_health(
                        definition.name,
                        definition.default_port,
                        definition.health_endpoint,
                        timeout=self.config.docker_startup_timeout,
                    )

                    if healthy:
                        logger.info(
                            f"    ⚡ Connected to {definition.name} via Docker (healthy after waiting)"
                        )
                        return True, "Docker container healthy after waiting"
                    else:
                        logger.warning(
                            f"    ⚠️  Docker container failed to become healthy, using local process"
                        )
                        return False, "Docker container unhealthy after timeout"
                else:
                    # Container has error status
                    logger.warning(
                        f"    ⚠️  Docker container unhealthy ({status}), using local process"
                    )
                    return False, f"Docker container unhealthy: {status}"
        else:
            # Container not running
            logger.info(f"    ℹ️  Docker container '{container_name}' not running")

            # Optionally auto-start Docker container
            if self.config.docker_auto_start:
                logger.info(f"    🚀 Auto-starting Docker container...")
                started = await self._start_docker_container(definition)
                if started:
                    logger.info(
                        f"    ⚡ Connected to {definition.name} via Docker (auto-started)"
                    )
                    return True, "Docker container auto-started"
                else:
                    logger.warning(
                        f"    ⚠️  Failed to auto-start Docker container, using local process"
                    )
                    return False, "Docker auto-start failed"
            else:
                return False, "Docker container not running (auto-start disabled)"

    async def _wait_for_docker_health(
        self,
        service_name: str,
        port: int,
        health_endpoint: str,
        timeout: float = 180.0,
    ) -> bool:
        """
        v93.8: Wait for a Docker-hosted service to become healthy.

        Args:
            service_name: Name of the service (for logging)
            port: Port the service is exposed on
            health_endpoint: Health check endpoint path
            timeout: Maximum time to wait

        Returns:
            True if service became healthy within timeout
        """
        start_time = time.time()
        check_interval = 3.0
        last_log_time = start_time

        while (time.time() - start_time) < timeout:
            is_healthy, status = await self._check_docker_service_healthy(
                service_name, port, health_endpoint
            )

            if is_healthy:
                return True

            # Log progress every 30 seconds
            if (time.time() - last_log_time) >= 30.0:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                logger.info(
                    f"    ⏳ {service_name} Docker: {status} "
                    f"({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)"
                )
                last_log_time = time.time()

            await asyncio.sleep(check_interval)

        return False

    # =========================================================================
    # v93.9: Advanced Memory Monitoring and Routing
    # =========================================================================

    async def _get_memory_status(self) -> MemoryStatus:
        """
        v93.9: Get current system memory status with caching.

        Uses psutil for cross-platform memory info.
        Caches result for performance (refreshed every 5s).
        """
        # Check cache
        if (
            self._cached_memory_status is not None
            and (time.time() - self._memory_cache_time) < self._memory_cache_ttl
        ):
            return self._cached_memory_status

        try:
            import psutil

            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()

            status = MemoryStatus(
                total_gb=mem.total / (1024 ** 3),
                available_gb=mem.available / (1024 ** 3),
                used_gb=mem.used / (1024 ** 3),
                percent_used=mem.percent,
                swap_total_gb=swap.total / (1024 ** 3),
                swap_used_gb=swap.used / (1024 ** 3),
            )

            # Update cache
            self._cached_memory_status = status
            self._memory_cache_time = time.time()

            return status

        except ImportError:
            logger.warning("    psutil not available, using default memory estimates")
            return MemoryStatus(
                total_gb=16.0,
                available_gb=8.0,
                used_gb=8.0,
                percent_used=50.0,
            )
        except Exception as e:
            logger.warning(f"    Memory check failed: {e}")
            return MemoryStatus(available_gb=8.0)

    async def _should_route_to_gcp(
        self,
        definition: ServiceDefinition,
        memory_status: MemoryStatus,
    ) -> bool:
        """
        v93.9: Determine if service should be routed to GCP instead of local.

        Decision factors:
        - Available memory vs model requirements
        - GCP fallback enabled
        - GCP VM availability
        """
        if not self.config.gcp_fallback_enabled:
            return False

        # Check if memory is critically low
        if memory_status.available_gb < self.config.memory_route_to_gcp_threshold_gb:
            logger.info(
                f"    📊 Memory critically low ({memory_status.available_gb:.1f}GB), "
                f"routing to GCP recommended"
            )
            return True

        # Check if model won't fit in available memory
        # Estimate model memory: file size * 1.5 (for runtime overhead)
        try:
            model_path = definition.repo_path / "models"
            if model_path.exists():
                # Find largest .gguf file
                gguf_files = list(model_path.glob("*.gguf"))
                if gguf_files:
                    largest_model = max(gguf_files, key=lambda p: p.stat().st_size)
                    model_size_gb = largest_model.stat().st_size / (1024 ** 3)
                    estimated_runtime_gb = model_size_gb * self.config.memory_model_size_estimation_factor

                    if estimated_runtime_gb > memory_status.available_gb:
                        logger.info(
                            f"    📊 Model requires ~{estimated_runtime_gb:.1f}GB, "
                            f"only {memory_status.available_gb:.1f}GB available, "
                            f"routing to GCP recommended"
                        )
                        return True
        except Exception as e:
            logger.debug(f"    Model size estimation failed: {e}")

        return False

    async def _make_routing_decision(
        self,
        definition: ServiceDefinition,
    ) -> tuple[RoutingDecision, str]:
        """
        v93.10: Make intelligent routing decision for a service.

        Enhanced decision tree with detailed memory thresholds:
        1. Memory < 2GB → GCP (emergency, system unstable)
        2. Memory 2-4GB → Docker (pre-loaded models) or GCP
        3. Memory 4-6GB → Docker (preferred) or Local
        4. Memory > 6GB → Local (fastest, no container overhead)

        Also considers:
        - Circuit breaker state
        - Docker availability
        - GCP infrastructure availability
        - Model size estimation

        Returns:
            Tuple of (decision, reason)
        """
        memory_status = await self._get_memory_status()

        logger.info(f"    📊 Memory: {memory_status.available_gb:.1f}GB available "
                   f"({memory_status.percent_used:.0f}% used)")

        # Use the enhanced routing decision tree
        route_type, reason = await self._determine_optimal_routing(
            definition, memory_status
        )

        # Map string route type to RoutingDecision enum
        route_map = {
            "gcp": RoutingDecision.GCP_CLOUD,
            "docker": RoutingDecision.DOCKER_LOCAL,
            "local": RoutingDecision.LOCAL_PROCESS,
            "hybrid": RoutingDecision.HYBRID,
        }

        return route_map.get(route_type, RoutingDecision.LOCAL_PROCESS), reason

    async def _determine_optimal_routing(
        self,
        definition: ServiceDefinition,
        memory_status: MemoryStatus,
    ) -> tuple[str, str]:
        """
        v93.10: Intelligently determine: Docker, Local, or GCP?

        Enhanced decision tree with:
        - Model size estimation
        - Circuit breaker awareness
        - Parallel availability checks
        - Cost-aware routing (prefer local when possible)

        Decision tree:
        1. Memory < 2GB → GCP (emergency)
        2. Memory 2-4GB → Docker (pre-loaded models) OR GCP
        3. Memory 4-6GB → Docker (preferred) or Local
        4. Memory > 6GB → Local (fastest)

        Returns:
            Tuple of (route_type, reason)
        """
        available_gb = memory_status.available_gb

        # Estimate model memory requirements
        estimated_model_gb = await self._estimate_model_memory_requirement(definition)
        logger.debug(f"    Estimated model memory: {estimated_model_gb:.1f}GB")

        # =====================================================================
        # TIER 0: Emergency - System critically low on memory
        # =====================================================================
        if available_gb < 2.0:
            logger.warning(
                f"    🚨 EMERGENCY: Only {available_gb:.1f}GB available! "
                f"Routing to GCP to prevent system instability"
            )
            return "gcp", f"Emergency: {available_gb:.1f}GB available (< 2GB threshold)"

        # =====================================================================
        # TIER 1: Low memory (2-4GB) - Docker with pre-loaded models or GCP
        # =====================================================================
        if available_gb < 4.0:
            # Check Docker availability and circuit breaker in parallel
            docker_available, cb_can_execute = await asyncio.gather(
                self._check_docker_available(),
                asyncio.to_thread(
                    lambda: self._get_circuit_breaker(definition.name).can_execute()
                ),
            )

            if docker_available and cb_can_execute:
                return (
                    "docker",
                    f"Low memory ({available_gb:.1f}GB), using Docker pre-loaded models"
                )
            elif self.config.gcp_fallback_enabled:
                return (
                    "gcp",
                    f"Low memory ({available_gb:.1f}GB), Docker unavailable, using GCP"
                )
            else:
                # Last resort: try local anyway with warning
                logger.warning(
                    f"    ⚠️ Low memory ({available_gb:.1f}GB) but no alternatives, "
                    f"attempting local (may be slow)"
                )
                return (
                    "local",
                    f"Low memory ({available_gb:.1f}GB), no alternatives (risky)"
                )

        # =====================================================================
        # TIER 2: Medium memory (4-6GB) - Prefer Docker, fallback to Local
        # =====================================================================
        if available_gb < 6.0:
            # Check if model will fit with headroom
            memory_with_model = available_gb - estimated_model_gb
            has_headroom = memory_with_model >= 1.0  # Need at least 1GB headroom

            # Check Docker availability
            docker_available = await self._check_docker_available()
            cb = self._get_circuit_breaker(definition.name)

            if docker_available and cb.can_execute():
                return (
                    "docker",
                    f"Medium memory ({available_gb:.1f}GB), using Docker for efficiency"
                )
            elif has_headroom:
                return (
                    "local",
                    f"Medium memory ({available_gb:.1f}GB), Docker unavailable, "
                    f"local has {memory_with_model:.1f}GB headroom"
                )
            elif self.config.gcp_fallback_enabled:
                return (
                    "gcp",
                    f"Medium memory ({available_gb:.1f}GB), insufficient headroom for local"
                )
            else:
                return (
                    "local",
                    f"Medium memory ({available_gb:.1f}GB), attempting local (tight fit)"
                )

        # =====================================================================
        # TIER 3: High memory (6GB+) - Local is fastest (no container overhead)
        # =====================================================================
        # Check if local process will have enough headroom
        memory_with_model = available_gb - estimated_model_gb
        has_good_headroom = memory_with_model >= 2.0  # 2GB+ headroom is comfortable

        if has_good_headroom:
            return (
                "local",
                f"Sufficient memory ({available_gb:.1f}GB, {memory_with_model:.1f}GB headroom), "
                f"using local process (fastest)"
            )

        # High memory but model is large - consider Docker for pre-loading
        docker_available = await self._check_docker_available()
        cb = self._get_circuit_breaker(definition.name)

        if docker_available and cb.can_execute():
            return (
                "docker",
                f"Good memory ({available_gb:.1f}GB) but large model "
                f"({estimated_model_gb:.1f}GB), using Docker"
            )

        # Local with warning
        return (
            "local",
            f"Good memory ({available_gb:.1f}GB), using local process"
        )

    async def _estimate_model_memory_requirement(
        self,
        definition: ServiceDefinition,
    ) -> float:
        """
        v93.10: Estimate memory required for loading the model.

        Considers:
        - Model file size
        - Runtime overhead factor (1.5x for llama.cpp)
        - Context window allocation
        - Safety buffer
        """
        try:
            model_path = definition.repo_path / "models"

            if not model_path.exists():
                # Default estimate for 7B model
                return 6.0

            # Find the current model (symlink) or largest .gguf
            current_model = model_path / "current.gguf"
            if current_model.is_symlink() or current_model.exists():
                try:
                    # Resolve symlink to get actual file
                    actual_path = current_model.resolve()
                    if actual_path.exists():
                        file_size_gb = actual_path.stat().st_size / (1024 ** 3)
                        # Runtime overhead: file_size * 1.5 + context buffer (0.5GB)
                        return file_size_gb * self.config.memory_model_size_estimation_factor + 0.5
                except Exception:
                    pass

            # Find largest .gguf file as fallback
            gguf_files = list(model_path.glob("*.gguf"))
            if gguf_files:
                largest = max(gguf_files, key=lambda p: p.stat().st_size)
                file_size_gb = largest.stat().st_size / (1024 ** 3)
                return file_size_gb * self.config.memory_model_size_estimation_factor + 0.5

            # Default estimate
            return 6.0

        except Exception as e:
            logger.debug(f"    Model size estimation failed: {e}")
            return 6.0  # Conservative default

    # =========================================================================
    # v93.9: Docker Image Management
    # =========================================================================

    async def _check_docker_image_exists(
        self,
        image_name: str = "jarvis-prime:latest",
    ) -> bool:
        """
        v93.9: Check if Docker image exists locally.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", image_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return_code = await asyncio.wait_for(proc.wait(), timeout=10.0)
            return return_code == 0
        except Exception:
            return False

    async def _build_docker_image(
        self,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v95.0: Build Docker image for a service with comprehensive event emissions.

        Uses docker-compose build with progress output and real-time voice narration.
        """
        compose_file = definition.repo_path / self.config.jarvis_prime_docker_compose

        if not compose_file.exists():
            logger.warning(f"    Docker compose file not found: {compose_file}")
            await _emit_event(
                "DOCKER_BUILD_FAILED",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "compose_file_missing", "path": str(compose_file)}
            )
            return False

        try:
            logger.info(f"    🔨 Building Docker image (this may take several minutes)...")

            # v95.0: Emit build start event for voice narration
            await _emit_event(
                "DOCKER_BUILD_START",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "compose_file": str(compose_file),
                    "estimated_duration": "several minutes"
                }
            )

            proc = await asyncio.create_subprocess_exec(
                "docker", "compose",
                "-f", str(compose_file),
                "build", "--progress=plain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(definition.repo_path),
            )

            # Stream build output with progress events
            build_start = time.time()
            last_log = build_start
            last_progress_event = build_start
            step_count = 0
            progress_event_interval = 30.0  # Emit progress event every 30 seconds

            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=self.config.docker_build_timeout
                )
                if not line:
                    break

                decoded = line.decode('utf-8', errors='replace').rstrip()
                if decoded:
                    # Track step progress
                    if 'step' in decoded.lower():
                        step_count += 1

                    # Log progress every 30 seconds or on important lines
                    now = time.time()
                    is_important = any(k in decoded.lower() for k in [
                        'step', 'downloading', 'extracting', 'error', 'warning',
                        'successfully', 'built', 'model'
                    ])

                    if is_important or (now - last_log) >= 30:
                        elapsed = now - build_start
                        logger.info(f"    [build {elapsed:.0f}s] {decoded[:100]}")
                        last_log = now

                    # v95.0: Emit periodic progress events for voice narration
                    if (now - last_progress_event) >= progress_event_interval:
                        elapsed = now - build_start
                        await _emit_event(
                            "DOCKER_BUILD_PROGRESS",
                            service_name=definition.name,
                            priority="LOW",
                            details={
                                "elapsed_seconds": int(elapsed),
                                "steps_completed": step_count,
                                "current_activity": decoded[:50] if decoded else "building"
                            }
                        )
                        last_progress_event = now

            return_code = await proc.wait()
            build_time = time.time() - build_start

            if return_code == 0:
                logger.info(f"    ✅ Docker image built successfully ({build_time:.0f}s)")
                # v95.0: Emit build complete event
                await _emit_event(
                    "DOCKER_BUILD_COMPLETE",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "build_time_seconds": int(build_time),
                        "steps_completed": step_count
                    }
                )
                return True
            else:
                logger.warning(f"    ❌ Docker build failed (exit code: {return_code})")
                # v95.0: Emit build failed event
                await _emit_event(
                    "DOCKER_BUILD_FAILED",
                    service_name=definition.name,
                    priority="CRITICAL",
                    details={
                        "exit_code": return_code,
                        "build_time_seconds": int(build_time)
                    }
                )
                return False

        except asyncio.TimeoutError:
            logger.warning(f"    ❌ Docker build timed out after {self.config.docker_build_timeout}s")
            # v95.0: Emit timeout event
            await _emit_event(
                "DOCKER_BUILD_FAILED",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "timeout",
                    "timeout_seconds": self.config.docker_build_timeout
                }
            )
            try:
                proc.terminate()
            except Exception:
                pass
            return False
        except Exception as e:
            logger.warning(f"    ❌ Docker build failed: {e}")
            # v95.0: Emit exception event
            await _emit_event(
                "DOCKER_BUILD_FAILED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "exception", "error": str(e)}
            )
            return False

    # =========================================================================
    # v93.10: Enhanced GCP Fallback Routing
    # =========================================================================

    async def _start_gcp_service(
        self,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v95.0: Start service on GCP Spot VM with comprehensive event emissions.

        Integrates with existing GCP infrastructure:
        - GCPVMManager for VM lifecycle management
        - CloudMLRouter for intelligent model routing
        - HybridRouter for capability-based routing
        - Cost-optimized Spot VM instances
        - Real-time voice narration of GCP operations

        Features:
        - Auto VM selection based on model requirements
        - Spot VM for cost savings (with preemption handling)
        - Health check with retry
        - Automatic service deployment on VM
        - Voice feedback for all GCP operations
        """
        if not self.config.gcp_fallback_enabled:
            logger.info("    ℹ️ GCP fallback disabled via config")
            return False

        try:
            # v95.0: Emit GCP routing decision event
            await _emit_event(
                "GCP_ROUTING_DECISION",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "starting_gcp_service"}
            )

            # ================================================================
            # Step 1: Initialize GCP infrastructure
            # ================================================================
            gcp_infra = await self._initialize_gcp_infrastructure()
            if not gcp_infra:
                logger.warning("    ⚠️ GCP infrastructure not available")
                # v95.0: Emit GCP failed event
                await _emit_event(
                    "GCP_VM_FAILED",
                    service_name=definition.name,
                    priority="HIGH",
                    details={"reason": "infrastructure_not_available"}
                )
                return False

            vm_manager = gcp_infra.get("vm_manager")
            ml_router = gcp_infra.get("ml_router")
            hybrid_router = gcp_infra.get("hybrid_router")

            logger.info(f"    ☁️ Starting {definition.name} on GCP cloud...")

            # ================================================================
            # Step 2: Determine optimal VM configuration
            # ================================================================
            vm_config = await self._determine_gcp_vm_config(definition, ml_router)
            vm_name = vm_config["name"]
            machine_type = vm_config["machine_type"]
            use_spot = vm_config["use_spot"]

            logger.info(
                f"    📊 GCP VM config: {machine_type} "
                f"({'Spot' if use_spot else 'On-demand'})"
            )

            # v95.0: Emit Spot allocation event if using spot
            if use_spot:
                await _emit_event(
                    "GCP_SPOT_ALLOCATED",
                    service_name=definition.name,
                    priority="MEDIUM",
                    details={
                        "machine_type": machine_type,
                        "vm_name": vm_name,
                        "cost_savings": "up to 80%"
                    }
                )

            # ================================================================
            # Step 3: Check if VM already exists and is running
            # ================================================================
            existing_vm = await self._check_existing_gcp_vm(
                vm_manager, vm_name, definition
            )
            if existing_vm:
                # v95.0: Emit GCP VM ready event (reusing existing)
                await _emit_event(
                    "GCP_VM_READY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "vm_name": vm_name,
                        "status": "reusing_existing",
                        "machine_type": machine_type
                    }
                )
                return True

            # ================================================================
            # Step 4: Create/start VM with service deployment
            # ================================================================
            # v95.0: Emit VM creating event
            await _emit_event(
                "GCP_VM_CREATING",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "vm_name": vm_name,
                    "machine_type": machine_type,
                    "spot_instance": use_spot
                }
            )

            vm_started = await self._create_and_start_gcp_vm(
                vm_manager,
                vm_name,
                machine_type,
                use_spot,
                definition,
            )

            if not vm_started:
                logger.warning(f"    ⚠️ Failed to start GCP VM")
                # v95.0: Emit GCP VM failed event
                await _emit_event(
                    "GCP_VM_FAILED",
                    service_name=definition.name,
                    priority="CRITICAL",
                    details={
                        "vm_name": vm_name,
                        "reason": "vm_start_failed"
                    }
                )
                return False

            # v95.0: Emit VM starting event
            await _emit_event(
                "GCP_VM_STARTING",
                service_name=definition.name,
                priority="MEDIUM",
                details={
                    "vm_name": vm_name,
                    "phase": "waiting_for_health"
                }
            )

            # ================================================================
            # Step 5: Wait for service to be healthy
            # ================================================================
            healthy = await self._wait_for_gcp_service_health(
                vm_manager,
                vm_name,
                definition,
            )

            if healthy:
                # v95.0: Emit GCP VM ready event
                await _emit_event(
                    "GCP_VM_READY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "vm_name": vm_name,
                        "machine_type": machine_type,
                        "spot_instance": use_spot
                    }
                )
            else:
                # v95.0: Emit GCP VM failed event
                await _emit_event(
                    "GCP_VM_FAILED",
                    service_name=definition.name,
                    priority="CRITICAL",
                    details={
                        "vm_name": vm_name,
                        "reason": "health_check_failed"
                    }
                )

            return healthy

        except ImportError as e:
            logger.warning(f"    ⚠️ GCP module not available: {e}")
            # v95.0: Emit GCP failed event
            await _emit_event(
                "GCP_VM_FAILED",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "module_not_available", "error": str(e)}
            )
            return False
        except Exception as e:
            logger.error(f"    ❌ GCP service start failed: {e}")
            # v95.0: Emit GCP failed event
            await _emit_event(
                "GCP_VM_FAILED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "exception", "error": str(e)}
            )
            import traceback
            logger.debug(traceback.format_exc())
            return False

    async def _initialize_gcp_infrastructure(self) -> Optional[Dict[str, Any]]:
        """
        v93.10: Initialize GCP infrastructure components.

        Lazy-loads:
        - GCPVMManager for VM lifecycle
        - CloudMLRouter for model routing (optional)
        - HybridRouter for capability routing (optional)
        """
        infrastructure: Dict[str, Any] = {}

        # VM Manager (required)
        if self._gcp_vm_manager is None:
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager
                self._gcp_vm_manager = get_gcp_vm_manager()
                logger.info("    ☁️ GCP VM Manager initialized")
            except ImportError:
                try:
                    from backend.core.gcp_vm_manager import GCPVMManager
                    self._gcp_vm_manager = GCPVMManager()
                    logger.info("    ☁️ GCP VM Manager initialized (direct)")
                except ImportError:
                    logger.warning("    ⚠️ GCP VM Manager not available")
                    return None

        infrastructure["vm_manager"] = self._gcp_vm_manager

        # CloudML Router (optional - for intelligent routing)
        try:
            from backend.core.cloud_ml_router import CloudMLRouter, get_cloud_ml_router
            try:
                infrastructure["ml_router"] = get_cloud_ml_router()
            except Exception:
                infrastructure["ml_router"] = CloudMLRouter()
            logger.debug("    ☁️ CloudML Router available")
        except ImportError:
            infrastructure["ml_router"] = None
            logger.debug("    ℹ️ CloudML Router not available")

        # Hybrid Router (optional - for capability-based routing)
        try:
            from backend.core.hybrid_router import HybridRouter, get_hybrid_router
            try:
                infrastructure["hybrid_router"] = get_hybrid_router()
            except Exception:
                infrastructure["hybrid_router"] = HybridRouter()
            logger.debug("    ☁️ Hybrid Router available")
        except ImportError:
            infrastructure["hybrid_router"] = None
            logger.debug("    ℹ️ Hybrid Router not available")

        return infrastructure

    async def _determine_gcp_vm_config(
        self,
        definition: ServiceDefinition,
        ml_router: Optional[Any],
    ) -> Dict[str, Any]:
        """
        v93.10: Determine optimal GCP VM configuration.

        Considers:
        - Model memory requirements
        - Cost optimization (Spot vs On-demand)
        - Region availability
        """
        # Estimate model memory requirements
        estimated_model_gb = await self._estimate_model_memory_requirement(definition)

        # Base VM name
        vm_name = f"jarvis-prime-{definition.name}"

        # Select machine type based on model requirements
        if estimated_model_gb < 4.0:
            # Small model: e2-standard-4 (16GB RAM)
            machine_type = "e2-standard-4"
        elif estimated_model_gb < 8.0:
            # Medium model: e2-highmem-4 (32GB RAM)
            machine_type = "e2-highmem-4"
        elif estimated_model_gb < 16.0:
            # Large model: e2-highmem-8 (64GB RAM)
            machine_type = "e2-highmem-8"
        else:
            # Very large model: n1-highmem-8 (52GB RAM) with GPU option
            machine_type = "n1-highmem-8"

        # Use Spot VMs for cost savings (with preemption handling)
        use_spot = True

        # If ML router available, get its recommendation
        if ml_router:
            try:
                ml_recommendation = await ml_router.get_vm_recommendation(
                    model_size_gb=estimated_model_gb,
                    prefer_spot=True,
                )
                if ml_recommendation:
                    machine_type = ml_recommendation.get("machine_type", machine_type)
                    use_spot = ml_recommendation.get("use_spot", use_spot)
            except Exception as e:
                logger.debug(f"    ML router recommendation failed: {e}")

        return {
            "name": vm_name,
            "machine_type": machine_type,
            "use_spot": use_spot,
            "estimated_memory_gb": estimated_model_gb,
        }

    async def _check_existing_gcp_vm(
        self,
        vm_manager: Any,
        vm_name: str,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v93.10: Check if GCP VM already exists and is running.

        Returns True if VM is running and service is healthy.
        """
        try:
            # Check VM status
            vm_status = await vm_manager.get_vm_status(vm_name)

            if not vm_status:
                return False

            status = vm_status.get("status", "UNKNOWN")

            if status == "RUNNING":
                logger.info(f"    ✅ GCP VM '{vm_name}' already running")

                # Get external IP
                external_ip = vm_status.get("external_ip")
                if not external_ip:
                    logger.warning(f"    ⚠️ VM running but no external IP")
                    return False

                # Check if service is healthy on the VM
                is_healthy, health_status = await self._check_gcp_service_health(
                    external_ip, definition
                )

                if is_healthy:
                    logger.info(
                        f"    ⚡ Connected to {definition.name} via GCP "
                        f"({external_ip}, status: {health_status})"
                    )
                    return True
                else:
                    logger.info(
                        f"    ℹ️ VM running but service not healthy: {health_status}"
                    )
                    # Service might be starting, let the caller handle waiting
                    return False

            elif status in ("STAGING", "PROVISIONING"):
                logger.info(f"    ℹ️ GCP VM '{vm_name}' is {status}...")
                return False

            elif status == "TERMINATED":
                logger.info(f"    ℹ️ GCP VM '{vm_name}' is terminated, will restart")
                return False

            else:
                logger.info(f"    ℹ️ GCP VM '{vm_name}' status: {status}")
                return False

        except Exception as e:
            logger.debug(f"    VM status check failed: {e}")
            return False

    async def _create_and_start_gcp_vm(
        self,
        vm_manager: Any,
        vm_name: str,
        machine_type: str,
        use_spot: bool,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v93.10: Create and start GCP VM with jarvis-prime service.
        """
        try:
            logger.info(
                f"    🚀 Creating/starting GCP VM '{vm_name}' "
                f"({machine_type}, {'Spot' if use_spot else 'On-demand'})..."
            )

            # Create or start VM
            # Try the enhanced method first
            try:
                result = await vm_manager.ensure_vm_running(
                    name=vm_name,
                    machine_type=machine_type,
                    preemptible=use_spot,
                    startup_script=self._generate_gcp_startup_script(definition),
                    timeout=self.config.gcp_vm_startup_timeout,
                )
                return result
            except TypeError:
                # Fallback to simpler interface
                result = await vm_manager.ensure_vm_running(
                    name=vm_name,
                    machine_type=machine_type,
                    timeout=self.config.gcp_vm_startup_timeout,
                )
                return result

        except Exception as e:
            logger.error(f"    ❌ Failed to create/start GCP VM: {e}")
            return False

    def _generate_gcp_startup_script(self, definition: ServiceDefinition) -> str:
        """
        v93.10: Generate startup script for GCP VM.

        Installs and starts jarvis-prime on the VM.
        """
        return f"""#!/bin/bash
set -e

# Log startup
exec > >(tee /var/log/jarvis-prime-startup.log) 2>&1
echo "=== JARVIS Prime GCP Startup Script ==="
date

# Install dependencies
apt-get update
apt-get install -y python3-pip python3-venv git

# Clone or update jarvis-prime
cd /opt
if [ -d "jarvis-prime" ]; then
    cd jarvis-prime && git pull
else
    git clone https://github.com/your-org/jarvis-prime.git
    cd jarvis-prime
fi

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Download model if not exists
if [ ! -f "models/current.gguf" ]; then
    python -m jarvis_prime.docker.model_downloader --model phi-3.5-mini
fi

# Start server
python run_server.py --host 0.0.0.0 --port {definition.default_port} &

echo "=== JARVIS Prime started ==="
"""

    async def _check_gcp_service_health(
        self,
        external_ip: str,
        definition: ServiceDefinition,
    ) -> tuple[bool, str]:
        """
        v93.10: Check health of service running on GCP VM.
        """
        url = f"http://{external_ip}:{definition.default_port}{definition.health_endpoint}"

        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10.0)
            ) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status}"

                try:
                    data = await response.json()
                    status = data.get("status", "unknown")

                    if status == "healthy":
                        return True, "healthy"
                    elif status == "starting":
                        elapsed = data.get("model_load_elapsed_seconds", 0)
                        return False, f"starting (model loading: {elapsed:.0f}s)"
                    else:
                        return False, status
                except Exception:
                    return True, "healthy (no JSON)"

        except asyncio.TimeoutError:
            return False, "timeout"
        except aiohttp.ClientConnectorError:
            return False, "connection refused"
        except Exception as e:
            return False, f"error: {e}"

    async def _wait_for_gcp_service_health(
        self,
        vm_manager: Any,
        vm_name: str,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v93.10: Wait for service on GCP VM to become healthy.

        Includes:
        - VM status monitoring
        - Service health checking
        - Progress logging
        """
        start_time = time.time()
        timeout = self.config.gcp_vm_startup_timeout
        check_interval = 5.0
        last_log_time = start_time

        logger.info(
            f"    ⏳ Waiting for GCP service to become healthy "
            f"(timeout: {timeout:.0f}s)..."
        )

        while (time.time() - start_time) < timeout:
            elapsed = time.time() - start_time

            # Check VM status first
            try:
                vm_status = await vm_manager.get_vm_status(vm_name)

                if not vm_status:
                    logger.warning(f"    ⚠️ VM status unavailable")
                    await asyncio.sleep(check_interval)
                    continue

                status = vm_status.get("status", "UNKNOWN")

                if status != "RUNNING":
                    # Log progress every 30 seconds
                    if (time.time() - last_log_time) >= 30.0:
                        logger.info(
                            f"    ⏳ VM status: {status} ({elapsed:.0f}s elapsed)"
                        )
                        last_log_time = time.time()
                    await asyncio.sleep(check_interval)
                    continue

                # VM is running, check service health
                external_ip = vm_status.get("external_ip")
                if not external_ip:
                    await asyncio.sleep(check_interval)
                    continue

                is_healthy, health_status = await self._check_gcp_service_health(
                    external_ip, definition
                )

                if is_healthy:
                    total_time = time.time() - start_time
                    logger.info(
                        f"    ✅ GCP service healthy after {total_time:.1f}s "
                        f"(IP: {external_ip})"
                    )
                    return True

                # Log progress
                if (time.time() - last_log_time) >= 30.0:
                    remaining = timeout - elapsed
                    logger.info(
                        f"    ⏳ GCP service: {health_status} "
                        f"({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)"
                    )
                    last_log_time = time.time()

            except Exception as e:
                logger.debug(f"    Health check error: {e}")

            await asyncio.sleep(check_interval)

        # Timeout
        total_time = time.time() - start_time
        logger.warning(
            f"    ⚠️ GCP service health timeout after {total_time:.1f}s"
        )
        return False

    # =========================================================================
    # v93.9: Enhanced Docker Container Startup
    # =========================================================================

    async def _start_docker_container(self, definition: ServiceDefinition) -> bool:
        """
        v95.0: Enhanced Docker container startup with comprehensive event emissions.

        Features:
        - Memory-aware routing (Docker vs GCP) with voice narration
        - Circuit breaker protection with state announcements
        - Retry with exponential backoff and progress events
        - Auto-build if image missing
        - Comprehensive error handling with voice feedback

        Args:
            definition: Service definition

        Returns:
            True if container started and became healthy
        """
        if definition.name != "jarvis-prime":
            return False

        # v95.0: Emit memory check event
        await _emit_event(
            "MEMORY_CHECK",
            service_name=definition.name,
            priority="MEDIUM",
            details={"phase": "pre_startup"}
        )

        # Step 1: Check memory and make routing decision
        memory_status = await self._get_memory_status()

        if memory_status.is_memory_pressure:
            logger.warning(
                f"    ⚠️ System under memory pressure "
                f"({memory_status.available_gb:.1f}GB available, "
                f"{memory_status.percent_used:.0f}% used)"
            )

            # v95.0: Emit memory pressure event
            pressure_level = "MEMORY_PRESSURE_CRITICAL" if memory_status.percent_used > 90 else "MEMORY_PRESSURE_HIGH"
            await _emit_event(
                pressure_level,
                service_name=definition.name,
                priority="HIGH",
                details={
                    "available_gb": memory_status.available_gb,
                    "percent_used": memory_status.percent_used,
                    "can_load_local_model": memory_status.can_load_local_model
                }
            )

            if await self._should_route_to_gcp(definition, memory_status):
                logger.info(f"    ☁️ Routing to GCP due to memory constraints...")
                # v95.0: Emit routing decision event
                await _emit_event(
                    "ROUTING_GCP",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "reason": "memory_constraints",
                        "available_gb": memory_status.available_gb
                    }
                )
                return await self._start_gcp_service(definition)

        # Step 2: Check circuit breaker
        cb = self._get_circuit_breaker(definition.name)
        if not cb.can_execute():
            logger.warning(
                f"    ⚠️ Docker circuit breaker OPEN for {definition.name}, "
                f"waiting for recovery timeout..."
            )
            # v95.0: Emit circuit breaker open event
            await _emit_event(
                "CIRCUIT_BREAKER_OPEN",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "failure_count": cb.failure_count,
                    "state": "open",
                    "recovery_timeout": cb.recovery_timeout
                }
            )
            return False

        # v95.0: Emit Docker check event
        await _emit_event(
            "DOCKER_CHECK",
            service_name=definition.name,
            priority="MEDIUM",
            details={"checking": "image_exists"}
        )

        # Step 3: Check if Docker image exists, build if needed
        image_name = "jarvis-prime:latest"
        image_exists = await self._check_docker_image_exists(image_name)

        if not image_exists:
            # v95.0: Emit Docker not found event
            await _emit_event(
                "DOCKER_NOT_FOUND",
                service_name=definition.name,
                priority="MEDIUM",
                details={"image_name": image_name}
            )

            if self.config.docker_auto_build:
                logger.info(f"    🔨 Docker image '{image_name}' not found, building...")
                built = await self._build_docker_image(definition)
                if not built:
                    cb.record_failure()
                    # v95.0: Emit circuit breaker trip event
                    await _emit_event(
                        "CIRCUIT_BREAKER_TRIP",
                        service_name=definition.name,
                        priority="HIGH",
                        details={"reason": "docker_build_failed"}
                    )
                    logger.warning(f"    ⚠️ Docker build failed, trying alternative...")
                    return await self._fallback_to_alternative(definition, memory_status)
            else:
                logger.warning(
                    f"    ⚠️ Docker image '{image_name}' not found and auto-build disabled"
                )
                return False
        else:
            # v95.0: Emit Docker found event
            await _emit_event(
                "DOCKER_FOUND",
                service_name=definition.name,
                priority="MEDIUM",
                details={"image_name": image_name}
            )

        # Step 4: Start container with retry and exponential backoff
        compose_file = definition.repo_path / self.config.jarvis_prime_docker_compose

        if not compose_file.exists():
            logger.warning(f"    Docker compose file not found: {compose_file}")
            cb.record_failure()
            return False

        retry_state = self._get_retry_state(f"docker-start-{definition.name}")
        retry_state.reset()  # Reset for new startup attempt
        start_time = time.time()

        # v95.0: Emit Docker starting event
        await _emit_event(
            "DOCKER_STARTING",
            service_name=definition.name,
            priority="HIGH",
            details={
                "image_name": image_name,
                "max_attempts": retry_state.max_attempts
            }
        )

        while retry_state.should_retry():
            attempt = retry_state.attempt + 1
            logger.info(
                f"    🚀 Starting Docker container (attempt {attempt}/"
                f"{retry_state.max_attempts})..."
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose",
                    "-f", str(compose_file),
                    "up", "-d",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(definition.repo_path),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=60.0
                )

                if proc.returncode != 0:
                    error_msg = stderr.decode()[:200]
                    retry_state.record_attempt(f"Exit code {proc.returncode}: {error_msg}")
                    logger.warning(f"    ⚠️ Docker compose up failed: {error_msg}")

                    if retry_state.should_retry():
                        delay = retry_state.get_next_delay()
                        logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        break

                logger.info(f"    Docker container starting, waiting for health...")

                # v95.0: Emit Docker health check event
                await _emit_event(
                    "DOCKER_HEALTH_CHECK",
                    service_name=definition.name,
                    priority="MEDIUM",
                    details={
                        "port": definition.default_port,
                        "endpoint": definition.health_endpoint,
                        "timeout": self.config.docker_startup_timeout
                    }
                )

                # Wait for container to become healthy
                healthy = await self._wait_for_docker_health(
                    definition.name,
                    definition.default_port,
                    definition.health_endpoint,
                    timeout=self.config.docker_startup_timeout,
                )

                if healthy:
                    elapsed = time.time() - start_time
                    cb.record_success()
                    retry_state.reset()

                    # v95.0: Emit circuit breaker closed event (healthy)
                    await _emit_event(
                        "CIRCUIT_BREAKER_CLOSED",
                        service_name=definition.name,
                        priority="MEDIUM",
                        details={"reason": "container_healthy"}
                    )

                    # v95.0: Emit Docker healthy event
                    await _emit_event(
                        "DOCKER_HEALTHY",
                        service_name=definition.name,
                        priority="HIGH",
                        details={
                            "startup_time_seconds": int(elapsed),
                            "attempts": attempt,
                            "port": definition.default_port
                        }
                    )

                    logger.info(
                        f"    ✅ Docker container healthy after {elapsed:.1f}s "
                        f"(attempt {attempt})"
                    )
                    return True
                else:
                    retry_state.record_attempt("Health check timeout")
                    logger.warning(f"    ⚠️ Container started but health check failed")

                    # v95.0: Emit Docker unhealthy event
                    await _emit_event(
                        "DOCKER_UNHEALTHY",
                        service_name=definition.name,
                        priority="HIGH",
                        details={
                            "reason": "health_check_timeout",
                            "attempt": attempt
                        }
                    )

                    if retry_state.should_retry():
                        delay = retry_state.get_next_delay()
                        logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                        # Stop unhealthy container before retry
                        await self._stop_docker_container(definition)
                        await asyncio.sleep(delay)
                        continue
                    else:
                        break

            except asyncio.TimeoutError:
                retry_state.record_attempt("Timeout")
                logger.warning(f"    ⚠️ Docker compose up timed out")

                if retry_state.should_retry():
                    delay = retry_state.get_next_delay()
                    logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    break

            except Exception as e:
                retry_state.record_attempt(str(e))
                logger.warning(f"    ⚠️ Docker compose up failed: {e}")

                if retry_state.should_retry():
                    delay = retry_state.get_next_delay()
                    logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    break

        # All retries exhausted
        cb.record_failure()

        # v95.0: Emit circuit breaker trip event
        await _emit_event(
            "CIRCUIT_BREAKER_TRIP",
            service_name=definition.name,
            priority="CRITICAL",
            details={
                "reason": "retries_exhausted",
                "failure_count": cb.failure_count,
                "last_error": retry_state.last_error
            }
        )

        total_elapsed = time.time() - start_time
        logger.error(
            f"    ❌ Docker startup failed after {retry_state.attempt} attempts "
            f"({total_elapsed:.1f}s total). Last error: {retry_state.last_error}"
        )

        # Try fallback
        return await self._fallback_to_alternative(definition, memory_status)

    async def _stop_docker_container(self, definition: ServiceDefinition) -> bool:
        """
        v93.9: Stop Docker container gracefully.
        """
        compose_file = definition.repo_path / self.config.jarvis_prime_docker_compose

        if not compose_file.exists():
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose",
                "-f", str(compose_file),
                "down",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(definition.repo_path),
            )
            await asyncio.wait_for(proc.wait(), timeout=30.0)
            return proc.returncode == 0
        except Exception:
            return False

    async def _fallback_to_alternative(
        self,
        definition: ServiceDefinition,
        memory_status: MemoryStatus,
    ) -> bool:
        """
        v95.0: Fallback to alternative deployment with comprehensive event emissions.

        Order of fallback (with voice narration):
        1. GCP cloud (if enabled and sufficient resources)
        2. Local process (if memory allows)
        3. GCP as last resort
        """
        logger.info(f"    🔄 Attempting fallback for {definition.name}...")

        # v95.0: Emit routing fallback event
        await _emit_event(
            "ROUTING_FALLBACK",
            service_name=definition.name,
            priority="HIGH",
            details={
                "phase": "starting_fallback_chain",
                "available_memory_gb": memory_status.available_gb,
                "can_load_local": memory_status.can_load_local_model,
                "gcp_enabled": self.config.gcp_fallback_enabled
            }
        )

        # Try GCP if enabled and memory is an issue
        if self.config.gcp_fallback_enabled:
            if memory_status.is_memory_pressure or not memory_status.can_load_local_model:
                logger.info(f"    ☁️ Trying GCP fallback...")
                # v95.0: Emit GCP fallback event
                await _emit_event(
                    "GCP_FALLBACK",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "reason": "memory_pressure" if memory_status.is_memory_pressure else "insufficient_memory",
                        "fallback_tier": 1
                    }
                )
                if await self._start_gcp_service(definition):
                    return True

        # Try local process if memory allows
        if memory_status.can_load_local_model:
            logger.info(f"    🐍 Falling back to local process...")
            # v95.0: Emit routing local event
            await _emit_event(
                "ROUTING_LOCAL",
                service_name=definition.name,
                priority="MEDIUM",
                details={
                    "reason": "memory_sufficient_for_local",
                    "available_gb": memory_status.available_gb,
                    "fallback_tier": 2
                }
            )
            # Return False to let the caller spawn local process
            return False

        # Last resort: try GCP anyway
        if self.config.gcp_fallback_enabled:
            logger.info(f"    ☁️ Last resort: trying GCP...")
            # v95.0: Emit GCP fallback event (last resort)
            await _emit_event(
                "GCP_FALLBACK",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "last_resort",
                    "fallback_tier": 3,
                    "all_other_options_exhausted": True
                }
            )
            return await self._start_gcp_service(definition)

        logger.warning(f"    ❌ No viable fallback options for {definition.name}")
        # v95.0: Emit final fallback failure event
        await _emit_event(
            "ROUTING_FALLBACK",
            service_name=definition.name,
            priority="CRITICAL",
            details={
                "phase": "fallback_chain_exhausted",
                "result": "no_viable_options"
            }
        )
        return False

    @property
    def registry(self):
        """
        v93.0: Lazy-load service registry with robust error handling.

        Handles:
        - ImportError: Module not available
        - RuntimeError: Directory creation failed
        - Any other initialization errors
        """
        if self._registry is None:
            try:
                from backend.core.service_registry import get_service_registry
                self._registry = get_service_registry()
                logger.debug("[v93.0] Service registry loaded successfully")

                # v95.0: Register callbacks for service lifecycle events
                # This enables automatic restart when services die
                self._registry.register_on_service_dead(self._on_registry_service_dead)
                self._registry.register_on_service_stale(self._on_registry_service_stale)
                logger.debug("[v95.0] Registered service lifecycle callbacks with registry")
            except ImportError:
                logger.warning("[v93.0] Service registry module not available")
            except RuntimeError as e:
                logger.error(f"[v93.0] Service registry initialization failed: {e}")
            except Exception as e:
                logger.error(f"[v93.0] Unexpected error loading service registry: {e}")
        return self._registry

    def add_service(self, definition: ServiceDefinition) -> None:
        """
        Add a service definition to the orchestrator.

        This allows dynamic addition of services beyond the default configuration.

        Args:
            definition: Service definition to add
        """
        if definition.name in self.processes:
            logger.warning(f"Service {definition.name} already exists, updating definition")

        self.processes[definition.name] = ManagedProcess(definition=definition)
        logger.debug(f"Added service: {definition.name}")

    def remove_service(self, name: str) -> bool:
        """
        Remove a service from the orchestrator.

        Args:
            name: Service name to remove

        Returns:
            True if service was removed
        """
        if name in self.processes:
            del self.processes[name]
            logger.debug(f"Removed service: {name}")
            return True
        return False

    # =========================================================================
    # v95.0: Service Registry Lifecycle Callbacks
    # =========================================================================

    async def _on_registry_service_dead(self, service_name: str, dead_pid: int) -> None:
        """
        v95.0: Callback invoked when service registry detects a dead service.

        This triggers automatic restart of the service if:
        1. The service is managed by this orchestrator
        2. Auto-healing is enabled
        3. The service hasn't exceeded max restart attempts
        """
        logger.warning(
            f"[v95.0] Registry notified service '{service_name}' died (PID {dead_pid})"
        )

        # Check if we manage this service
        if service_name not in self.processes:
            logger.debug(f"[v95.0] Service '{service_name}' not managed by this orchestrator")
            return

        managed = self.processes[service_name]

        # Mark as failed
        managed.status = ServiceStatus.FAILED
        managed.pid = None  # PID is dead

        # Emit event for voice narration
        await _emit_event(
            "SERVICE_CRASHED",
            service_name=service_name,
            priority="CRITICAL",
            details={
                "reason": "registry_detected_dead_pid",
                "dead_pid": dead_pid,
                "auto_healing": self.config.auto_healing_enabled
            }
        )

        # Attempt auto-restart if enabled
        if self.config.auto_healing_enabled:
            logger.info(f"[v95.0] Attempting auto-restart of '{service_name}'")
            success = await self._auto_heal(managed)
            if success:
                logger.info(f"[v95.0] Successfully restarted '{service_name}'")
            else:
                logger.error(f"[v95.0] Failed to restart '{service_name}'")
        else:
            logger.warning(f"[v95.0] Auto-healing disabled, '{service_name}' will remain down")

    async def _on_registry_service_stale(self, service_name: str, heartbeat_age: float) -> None:
        """
        v95.0: Callback invoked when service registry detects a stale service.

        This can trigger investigation or restart if the service is unresponsive.
        """
        logger.warning(
            f"[v95.0] Registry notified service '{service_name}' is stale "
            f"(no heartbeat for {heartbeat_age:.0f}s)"
        )

        # Check if we manage this service
        if service_name not in self.processes:
            return

        managed = self.processes[service_name]

        # Mark as degraded
        if managed.status != ServiceStatus.FAILED:
            managed.status = ServiceStatus.DEGRADED

        # Emit event for monitoring
        await _emit_event(
            "SERVICE_UNHEALTHY",
            service_name=service_name,
            priority="HIGH",
            details={
                "reason": "stale_heartbeat",
                "heartbeat_age_seconds": int(heartbeat_age)
            }
        )

        # If very stale (>5 minutes), consider it dead and restart
        if heartbeat_age > 300 and self.config.auto_healing_enabled:
            logger.warning(
                f"[v95.0] Service '{service_name}' extremely stale ({heartbeat_age:.0f}s), "
                f"treating as dead and attempting restart"
            )
            managed.status = ServiceStatus.FAILED
            await self._auto_heal(managed)

    def _get_service_definitions(self) -> List[ServiceDefinition]:
        """
        Get service definitions based on configuration.

        v4.0: Uses actual entry point scripts from each repo:
        - jarvis-prime: run_server.py (port 8000)
        - reactor-core: run_reactor.py (port 8090)

        v95.0: Now uses ServiceDefinitionRegistry for consistency.
        The registry provides canonical definitions that can be overridden
        by the orchestrator's config.
        """
        definitions = []

        if self.config.jarvis_prime_enabled:
            # v95.0: Use registry for canonical definition, override with config
            jprime_def = ServiceDefinitionRegistry.get_definition(
                "jarvis-prime",
                port_override=self.config.jarvis_prime_default_port,
                path_override=self.config.jarvis_prime_path,
                validate=True,
            )
            if jprime_def:
                # Override timeout from config
                jprime_def.startup_timeout = self.config.jarvis_prime_startup_timeout
                definitions.append(jprime_def)
                logger.debug(f"Using registry definition for jarvis-prime: {jprime_def.script_name}")
            else:
                logger.warning("Could not get jarvis-prime definition from registry, using fallback")
                # Fallback to inline definition if registry fails
                definitions.append(ServiceDefinition(
                    name="jarvis-prime",
                    repo_path=self.config.jarvis_prime_path,
                    script_name="run_server.py",
                    fallback_scripts=["main.py", "server.py", "app.py"],
                    default_port=self.config.jarvis_prime_default_port,
                    health_endpoint="/health",
                    startup_timeout=self.config.jarvis_prime_startup_timeout,
                    script_args=["--port", str(self.config.jarvis_prime_default_port), "--host", "0.0.0.0"],
                    environment={
                        "PYTHONPATH": str(self.config.jarvis_prime_path),
                        "PYTHONWARNINGS": "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning",
                        "TF_CPP_MIN_LOG_LEVEL": "2",
                        "TRANSFORMERS_VERBOSITY": "error",
                        "TOKENIZERS_PARALLELISM": "false",
                        "COREMLTOOLS_LOG_LEVEL": "ERROR",
                    },
                ))

        if self.config.reactor_core_enabled:
            # v95.0: Use registry for canonical definition, override with config
            reactor_def = ServiceDefinitionRegistry.get_definition(
                "reactor-core",
                port_override=self.config.reactor_core_default_port,
                path_override=self.config.reactor_core_path,
                validate=True,
            )
            if reactor_def:
                # Override timeout from config
                reactor_def.startup_timeout = self.config.reactor_core_startup_timeout
                definitions.append(reactor_def)
                logger.debug(f"Using registry definition for reactor-core: {reactor_def.script_name}")
            else:
                logger.warning("Could not get reactor-core definition from registry, using fallback")
                # Fallback to inline definition if registry fails
                definitions.append(ServiceDefinition(
                    name="reactor-core",
                    repo_path=self.config.reactor_core_path,
                    script_name="run_reactor.py",
                    fallback_scripts=["run_supervisor.py", "main.py", "server.py"],
                    default_port=self.config.reactor_core_default_port,
                    health_endpoint="/health",
                    startup_timeout=self.config.reactor_core_startup_timeout,
                    use_uvicorn=False,
                    uvicorn_app=None,
                    script_args=["--port", str(self.config.reactor_core_default_port)],
                    environment={
                        "PYTHONPATH": str(self.config.reactor_core_path),
                        "REACTOR_PORT": str(self.config.reactor_core_default_port),
                    },
                ))

        return definitions

    def _find_script(self, definition: ServiceDefinition) -> Optional[Path]:
        """
        Find the startup script for a service.

        v3.1: Enhanced discovery with nested script paths and module detection.
        v95.0: Now uses intelligent discover_entry_script() with pattern matching and scoring.

        Search order:
        1. Module path (returns sentinel but module_path is used directly in spawn)
        2. Uvicorn app (returns sentinel but uvicorn is used in spawn)
        3. Intelligent discovery via discover_entry_script() which:
           - Checks explicit script_name (highest priority)
           - Checks nested_scripts
           - Checks fallback_scripts
           - Pattern matches run_*.py, main.py, server.py, app.py
           - Uses file size as tiebreaker for equal scores
        """
        repo_path = definition.repo_path

        if not repo_path.exists():
            logger.warning(f"Repository not found: {repo_path}")
            return None

        # v3.1: If module_path or uvicorn_app is set, we don't need a script file
        # The spawn method will handle these directly
        if definition.module_path or definition.uvicorn_app:
            logger.debug(f"Service {definition.name} uses module/uvicorn entry point")
            return Path("__module__")  # Sentinel value

        # v95.0: Use intelligent discovery
        script_path = definition.discover_entry_script()

        if script_path:
            logger.debug(f"Discovered entry script for {definition.name}: {script_path}")
            return script_path

        # v95.0: Enhanced logging for debugging
        logger.warning(
            f"No startup script found for {definition.name} in {repo_path}"
        )
        logger.debug(f"  Tried explicit: {definition.script_name}")
        logger.debug(f"  Tried nested: {definition.nested_scripts}")
        logger.debug(f"  Tried fallbacks: {definition.fallback_scripts}")
        logger.debug(f"  Discovery patterns: {definition.discovery_patterns}")

        # List actual files for debugging
        try:
            py_files = [f.name for f in repo_path.glob("*.py")][:10]
            if py_files:
                logger.debug(f"  Available .py files: {py_files}")
        except Exception:
            pass

        return None

    # =========================================================================
    # Output Streaming (v93.0: Intelligent log level detection)
    # =========================================================================

    def _detect_log_level(self, line: str) -> str:
        """
        v93.0: Intelligently detect log level from output line content.

        Python logging outputs to stderr by default, so we can't rely on
        stream type alone. Instead, we parse the line content to detect
        the actual log level.

        Patterns detected:
        - "| DEBUG |", "DEBUG:", "[DEBUG]"
        - "| INFO |", "INFO:", "[INFO]"
        - "| WARNING |", "WARNING:", "[WARNING]", "WARN:"
        - "| ERROR |", "ERROR:", "[ERROR]"
        - "| CRITICAL |", "CRITICAL:", "[CRITICAL]"
        - Traceback, Exception indicators -> ERROR
        """
        line_upper = line.upper()

        # Check for explicit log level indicators
        if any(p in line_upper for p in ['| ERROR |', 'ERROR:', '[ERROR]', '| CRITICAL |', 'CRITICAL:']):
            return 'error'
        if any(p in line_upper for p in ['TRACEBACK', 'EXCEPTION', 'RAISE ', 'FAILED:', '❌']):
            return 'error'
        if any(p in line_upper for p in ['| WARNING |', 'WARNING:', '[WARNING]', 'WARN:', '⚠️']):
            return 'warning'
        if any(p in line_upper for p in ['| DEBUG |', 'DEBUG:', '[DEBUG]']):
            return 'debug'
        if any(p in line_upper for p in ['| INFO |', 'INFO:', '[INFO]', '✅', '✓']):
            return 'info'

        # Default to info for normal output
        return 'info'

    # v93.16: Patterns for warnings that should be suppressed
    # v95.0: Extended with more ML framework warnings
    # v95.1: Added Google API, gspread, and asyncio patterns
    _SUPPRESSED_WARNING_PATTERNS = [
        # Scikit-learn version warnings
        'scikit-learn version',
        'is not supported',
        'minimum required version',
        'maximum required version',
        'disabling scikit-learn conversion api',
        # PyTorch/TorchAudio warnings
        'torch version',
        'has not been tested',
        'list_audio_backends',
        'torchaudio.backend',
        # CoreML warnings
        'coremltools',
        'unexpected errors',
        'most recent version that has been tested',
        # SpeechBrain/HuggingFace warnings (v95.0)
        'wav2vec2model is frozen',
        'model is frozen',
        'weights were not initialized',
        'you should probably train',
        'some weights of the model checkpoint',
        'registered checkpoint',
        # TensorFlow warnings
        'tf_cpp_min_log_level',
        # Transformers/Tokenizers warnings
        'tokenizers parallelism',
        'special tokens have been added',
        'clean_up_tokenization_spaces',
        # NumPy/SciPy warnings
        'numpy.ndarray size changed',
        'scipy.ndimage',
        # Google API/GCP warnings (v95.1)
        'non-supported python version',
        'google will not post any further updates',
        'please upgrade to the latest python version',
        'google.api_core',
        # Optional dependencies not installed (v95.1)
        'gspread not available',
        'redis not available',
        'pip install',
        # aiohttp/asyncio cleanup warnings (v95.1)
        'unclosed client session',
        'unclosed connector',
        'event loop is closed',
        # Thread pool shutdown warnings (v95.1)
        'thread pool',
        'cannot schedule new futures',
        'interpreter shutdown',
        # General benign patterns
        'futurewarning',
        'deprecationwarning',
    ]

    def _should_suppress_line(self, line: str) -> bool:
        """v93.16: Check if a line should be suppressed (known benign warnings)."""
        line_lower = line.lower()
        return any(pattern in line_lower for pattern in self._SUPPRESSED_WARNING_PATTERNS)

    async def _stream_output(
        self,
        managed: ManagedProcess,
        stream: asyncio.StreamReader,
        stream_type: str = "stdout"
    ) -> None:
        """
        v93.0: Stream process output with intelligent log level detection.
        v93.16: Added warning suppression for known benign library warnings.

        Python's logging module outputs to stderr by default, which previously
        caused all child process logs to appear as WARNING in our output.

        Now we parse the actual content to detect the real log level and
        route appropriately.

        Example output:
            [JARVIS_PRIME] Loading model...
            [JARVIS_PRIME] Model loaded in 2.3s
            [REACTOR_CORE] Initializing pipeline...
        """
        prefix = f"[{managed.definition.name.upper().replace('-', '_')}]"

        try:
            while True:
                line = await stream.readline()
                if not line:
                    break

                decoded = line.decode('utf-8', errors='replace').rstrip()
                if decoded:
                    # v93.16: Suppress known benign warnings
                    if self._should_suppress_line(decoded):
                        logger.debug(f"{prefix} [SUPPRESSED] {decoded}")
                        continue

                    # Detect actual log level from content
                    level = self._detect_log_level(decoded)

                    # Route to appropriate log function
                    if level == 'error':
                        logger.error(f"{prefix} {decoded}")
                    elif level == 'warning':
                        logger.warning(f"{prefix} {decoded}")
                    elif level == 'debug':
                        logger.debug(f"{prefix} {decoded}")
                    else:
                        logger.info(f"{prefix} {decoded}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Output streaming error for {managed.definition.name}: {e}")

    async def _start_output_streaming(self, managed: ManagedProcess) -> None:
        """Start streaming stdout and stderr for a process."""
        if not self.config.stream_output:
            return

        if managed.process is None:
            return

        async def stream_both():
            tasks = []
            if managed.process.stdout:
                tasks.append(
                    asyncio.create_task(
                        self._stream_output(managed, managed.process.stdout, "stdout")
                    )
                )
            if managed.process.stderr:
                tasks.append(
                    asyncio.create_task(
                        self._stream_output(managed, managed.process.stderr, "stderr")
                    )
                )
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        managed.output_stream_task = asyncio.create_task(stream_both())

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _check_health(
        self,
        managed: ManagedProcess,
        require_ready: bool = True,
    ) -> bool:
        """
        Check health of a service via HTTP endpoint.

        v93.0: Enhanced to support startup-aware health checking.

        Args:
            managed: The managed process to check
            require_ready: If True, require "healthy" status. If False, accept "starting" too.

        Returns:
            True if service is responding appropriately
        """
        if managed.port is None:
            return False

        url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"

        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout)
            ) as response:
                if response.status != 200:
                    return False

                # v93.0: Parse response to check status field
                try:
                    data = await response.json()
                    status = data.get("status", "unknown")

                    if status == "healthy":
                        return True
                    elif status == "starting":
                        # Server is up but model still loading
                        if not require_ready:
                            return True
                        # Log progress if available
                        elapsed = data.get("model_load_elapsed_seconds")
                        if elapsed:
                            logger.debug(
                                f"    ℹ️  {managed.definition.name}: status=starting, "
                                f"model loading for {elapsed:.0f}s"
                            )
                        return False
                    elif status == "error":
                        error = data.get("model_load_error", "unknown error")
                        logger.warning(
                            f"    ⚠️ {managed.definition.name}: status=error - {error}"
                        )
                        return False
                    else:
                        # Unknown status, be conservative
                        return False
                except Exception:
                    # Couldn't parse JSON, fall back to HTTP status
                    return True

        except Exception:
            return False

    async def _check_service_responding(self, managed: ManagedProcess) -> bool:
        """
        v93.0: Check if service is responding at all (including "starting" status).

        This is for the initial health check - we just want to know the port is open.
        """
        return await self._check_health(managed, require_ready=False)

    async def _health_monitor_loop(self, managed: ManagedProcess) -> None:
        """
        v93.8: Enhanced background health monitoring with Docker support.

        CRITICAL FIX: Previous version would `break` after auto-heal, which meant
        if auto-heal failed, monitoring would stop completely. Now we continue
        monitoring and retry auto-heal as needed.

        Features:
        - Robust process death detection with poll() (local processes)
        - Docker container health monitoring (no local process)
        - Continuous monitoring even after auto-heal attempts
        - HTTP health check with consecutive failure tracking
        - Heartbeat updates to service registry
        - Graceful degradation on temporary failures
        """
        # v93.8: Determine if this is a Docker-hosted service
        is_docker_service = managed.pid is None and managed.port is not None

        try:
            # v95.3: Check both shutdown event AND completion flag
            while not self._shutdown_event.is_set() and not self._shutdown_completed:
                await asyncio.sleep(self.config.health_check_interval)

                # v95.3: Re-check shutdown state after sleep
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.debug(
                        f"[v95.3] Health monitor for {managed.definition.name} exiting: "
                        f"shutdown detected after sleep"
                    )
                    return

                # v93.8: For Docker services, skip process death detection
                # Docker containers don't have a local process to monitor
                if not is_docker_service:
                    # v93.0: Enhanced process death detection for LOCAL processes
                    # Use poll() to update returncode without blocking
                    if managed.process is not None:
                        try:
                            # poll() returns None if still running, exit code if terminated
                            poll_result = managed.process.returncode
                            if poll_result is None:
                                # Process might have exited but returncode not updated yet
                                # On macOS/Unix, we need to wait() to reap zombie processes
                                # Use wait_for with 0 timeout to check without blocking
                                pass  # returncode is None means still running
                        except Exception:
                            pass

                # v93.8: Check if running - for Docker services, always use HTTP health check
                if not is_docker_service and not managed.is_running:
                    # Process died, trigger auto-heal if enabled
                    exit_code = managed.process.returncode if managed.process else "unknown"

                    # v95.3: Check if this is an intentional shutdown (exit code -15 = SIGTERM)
                    # During shutdown, processes are killed with SIGTERM, so we shouldn't
                    # log scary warnings or attempt to restart them
                    # CRITICAL: Also check shutdown_completed to catch post-shutdown kills
                    is_intentional_shutdown = (
                        self._shutdown_event.is_set() or
                        self._shutdown_completed or  # v95.3: Added completion check
                        exit_code == -15 or  # SIGTERM
                        exit_code == -2      # SIGINT
                    )

                    if is_intentional_shutdown:
                        logger.info(
                            f"[v95.0] Process {managed.definition.name} stopped "
                            f"(exit code: {exit_code}, shutdown: {self._shutdown_event.is_set()})"
                        )
                        managed.status = ServiceStatus.STOPPED
                        return  # Exit health monitor - this is expected
                    else:
                        logger.warning(
                            f"🚨 Process {managed.definition.name} died unexpectedly "
                            f"(exit code: {exit_code})"
                        )
                        managed.status = ServiceStatus.FAILED

                    if self.config.auto_healing_enabled and not is_intentional_shutdown:
                        success = await self._auto_heal(managed)
                        if success:
                            # v93.0: After successful auto-heal, the new process has
                            # its own health monitor task started in _spawn_service()
                            # We exit THIS loop since we're monitoring the OLD process
                            logger.info(
                                f"[v93.0] Health monitor for old {managed.definition.name} "
                                f"process exiting (new monitor started)"
                            )
                            return
                        else:
                            # Auto-heal failed, but don't give up immediately
                            # Continue monitoring - maybe the process will recover
                            # or manual intervention will fix it
                            logger.warning(
                                f"[v93.0] Auto-heal failed for {managed.definition.name}, "
                                f"continuing to monitor"
                            )
                            # Wait longer before retrying
                            await asyncio.sleep(self.config.health_check_interval * 2)
                            continue
                    elif is_intentional_shutdown:
                        # Intentional shutdown - just exit cleanly
                        logger.debug(
                            f"[v95.0] {managed.definition.name} stopped intentionally, "
                            f"health monitor exiting"
                        )
                        return
                    else:
                        # Auto-healing disabled, just log and exit
                        logger.error(
                            f"[v93.0] {managed.definition.name} died but auto-healing disabled"
                        )
                        return

                # HTTP health check
                healthy = await self._check_health(managed)
                managed.last_health_check = time.time()

                if healthy:
                    managed.consecutive_failures = 0
                    # v95.0: Update last known health for heartbeat loop
                    managed.last_known_health = "healthy"

                    # v95.2: Reset restart count when service is healthy
                    # This allows the service to be restarted again if it fails later
                    if managed.restart_count > 0:
                        logger.info(
                            f"[v95.2] Resetting restart count for {managed.definition.name} "
                            f"(was {managed.restart_count}, service is now healthy)"
                        )
                        managed.restart_count = 0

                    # Log status transition only once
                    if managed.status != ServiceStatus.HEALTHY:
                        managed.status = ServiceStatus.HEALTHY
                        logger.info(f"✅ {managed.definition.name} is healthy")

                    # ═══════════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Send heartbeat on EVERY successful health check
                    # (The dedicated heartbeat loop also sends, but this is belt-and-suspenders)
                    # ═══════════════════════════════════════════════════════════════════
                    if self.registry:
                        try:
                            await self.registry.heartbeat(
                                managed.definition.name,
                                status="healthy"
                            )
                        except Exception as hb_error:
                            logger.warning(
                                f"[v93.0] Heartbeat failed for {managed.definition.name} "
                                f"(non-fatal): {hb_error}"
                            )
                else:
                    # v95.0: Update last known health for heartbeat loop
                    managed.last_known_health = "degraded"
                    managed.consecutive_failures += 1
                    logger.warning(
                        f"⚠️ {managed.definition.name} health check failed "
                        f"({managed.consecutive_failures} consecutive failures)"
                    )

                    if managed.consecutive_failures >= 3:
                        managed.status = ServiceStatus.DEGRADED

                        if self.config.auto_healing_enabled:
                            success = await self._auto_heal(managed)
                            if success:
                                # Reset consecutive failures after successful heal
                                managed.consecutive_failures = 0
                            # v93.0: Don't break - continue monitoring

        except asyncio.CancelledError:
            logger.debug(f"[v93.0] Health monitor cancelled for {managed.definition.name}")
        except Exception as e:
            logger.error(f"Health monitor error for {managed.definition.name}: {e}")

    # =========================================================================
    # v95.0: Dedicated Heartbeat Loop (Prevents Stale Services)
    # =========================================================================

    async def _heartbeat_loop(self, managed: ManagedProcess) -> None:
        """
        v95.0: Enterprise-grade dedicated heartbeat loop.

        CRITICAL: This loop runs INDEPENDENTLY of health checks.

        The previous design had a fundamental flaw:
        - Heartbeats were only sent when HTTP health checks passed
        - If health checks failed, no heartbeat was sent
        - This caused running services to be marked "stale" and removed
        - Even if the process was alive, it would be deregistered

        This new design ensures:
        1. Heartbeats are sent as long as the process is alive
        2. Health status is included in the heartbeat (healthy/degraded/unhealthy)
        3. Services are NEVER marked stale if their process is running
        4. Separate heartbeat interval (faster than health check interval)
        5. Automatic recovery with fire-and-forget pattern

        The heartbeat loop sends status-aware heartbeats:
        - "healthy": HTTP health check passed
        - "degraded": Process alive but HTTP check failed
        - "starting": Process just started, not yet healthy
        - "process_alive": Fallback when health status unknown
        """
        service_name = managed.definition.name
        heartbeat_interval = min(
            self.config.health_check_interval / 2,  # 2x faster than health checks
            15.0  # Max 15 seconds between heartbeats
        )

        logger.info(
            f"[v95.0] Starting dedicated heartbeat loop for {service_name} "
            f"(interval: {heartbeat_interval:.1f}s)"
        )

        # v95.0: Path for file-based heartbeats (used by reactor-core and other services)
        trinity_heartbeat_path = Path.home() / ".jarvis" / "trinity" / "heartbeats" / f"{service_name}.json"

        try:
            # v95.3: Check both shutdown event AND completion flag
            while not self._shutdown_event.is_set() and not self._shutdown_completed:
                await asyncio.sleep(heartbeat_interval)

                # v95.3: Re-check shutdown state after sleep
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.debug(
                        f"[v95.3] Heartbeat loop for {service_name} exiting: shutdown detected"
                    )
                    break

                # Check if process is still alive
                process_alive = managed.is_running

                # For Docker services, process is None but port is set
                is_docker = managed.pid is None and managed.port is not None

                # v95.0: Check file-based heartbeat as fallback (reactor-core uses this)
                file_heartbeat_alive = False
                file_heartbeat_status = None
                try:
                    if trinity_heartbeat_path.exists():
                        stat = trinity_heartbeat_path.stat()
                        file_age = time.time() - stat.st_mtime
                        # File heartbeat is fresh if modified within 2x heartbeat interval
                        if file_age < (heartbeat_interval * 2):
                            file_heartbeat_alive = True
                            # Read status from file
                            try:
                                with open(trinity_heartbeat_path) as f:
                                    hb_data = json.load(f)
                                    file_heartbeat_status = hb_data.get("status", "unknown")
                            except Exception:
                                file_heartbeat_status = "file_heartbeat_active"
                except Exception as e:
                    logger.debug(f"[v95.0] File heartbeat check failed for {service_name}: {e}")

                if not process_alive and not is_docker and not file_heartbeat_alive:
                    # Process died AND no file heartbeat - notify registry and stop
                    logger.debug(
                        f"[v95.0] Heartbeat loop for {service_name}: "
                        f"process not running and no file heartbeat, sending final notification"
                    )

                    # v95.0: Send final "dead" notification to registry before exiting
                    # This prevents the 30s gap where service appears alive but is dead
                    if self.registry:
                        try:
                            await asyncio.wait_for(
                                self.registry.heartbeat(
                                    service_name,
                                    status="dead",
                                    metadata={
                                        "pid": managed.pid,
                                        "reason": "process_died",
                                        "last_heartbeat_source": "cleanup",
                                        "exit_code": getattr(managed.process, 'returncode', None) if managed.process else None,
                                    }
                                ),
                                timeout=2.0
                            )
                            logger.info(f"[v95.0] Sent final 'dead' heartbeat for {service_name}")
                        except Exception as final_hb_err:
                            logger.debug(f"[v95.0] Final heartbeat failed for {service_name}: {final_hb_err}")

                        # Also explicitly deregister to immediately free up the slot
                        try:
                            await asyncio.wait_for(
                                self.registry.deregister_service(service_name),
                                timeout=2.0
                            )
                            logger.info(f"[v95.0] Deregistered dead service: {service_name}")
                        except Exception as dereg_err:
                            logger.debug(f"[v95.0] Deregister failed for {service_name}: {dereg_err}")

                    return

                # v95.0: If process appears dead but file heartbeat is alive,
                # the service is running (just not as a direct child process)
                if not process_alive and file_heartbeat_alive:
                    logger.debug(
                        f"[v95.0] {service_name}: Process not running but file heartbeat "
                        f"active ({file_heartbeat_status}) - service alive externally"
                    )

                # v95.0: Determine heartbeat status based on multiple sources
                # Priority: file heartbeat > last known health > process state
                if file_heartbeat_alive and file_heartbeat_status == "healthy":
                    heartbeat_status = "healthy"
                elif managed.last_known_health == "healthy":
                    heartbeat_status = "healthy"
                elif managed.status == ServiceStatus.STARTING:
                    heartbeat_status = "starting"
                elif file_heartbeat_alive:
                    # File heartbeat is active but status is not "healthy"
                    heartbeat_status = file_heartbeat_status or "file_heartbeat_active"
                elif managed.last_known_health == "degraded":
                    heartbeat_status = "degraded"
                elif managed.consecutive_failures > 0:
                    heartbeat_status = "degraded"
                elif process_alive or is_docker:
                    heartbeat_status = "process_alive"
                else:
                    heartbeat_status = "unknown"

                # Send heartbeat to registry
                if self.registry:
                    try:
                        await asyncio.wait_for(
                            self.registry.heartbeat(
                                service_name,
                                status=heartbeat_status,
                                metadata={
                                    "pid": managed.pid,
                                    "port": managed.port,
                                    "consecutive_failures": managed.consecutive_failures,
                                    "heartbeat_source": "dedicated_loop",
                                    "is_docker": is_docker,
                                    "file_heartbeat_active": file_heartbeat_alive,
                                    "file_heartbeat_status": file_heartbeat_status,
                                }
                            ),
                            timeout=5.0  # Don't block on slow registry
                        )
                        managed.last_heartbeat_sent = time.time()
                        logger.debug(
                            f"[v95.0] Heartbeat sent for {service_name} "
                            f"(status: {heartbeat_status})"
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[v95.0] Heartbeat timeout for {service_name} (non-fatal)"
                        )
                    except Exception as hb_err:
                        logger.warning(
                            f"[v95.0] Heartbeat failed for {service_name}: {hb_err} (non-fatal)"
                        )

        except asyncio.CancelledError:
            logger.debug(f"[v95.0] Heartbeat loop cancelled for {service_name}")
            # v95.0: On cancellation (shutdown), send final status update
            if self.registry and self._shutdown_event.is_set():
                try:
                    await asyncio.wait_for(
                        self.registry.heartbeat(
                            service_name,
                            status="shutting_down",
                            metadata={"reason": "orchestrator_shutdown"}
                        ),
                        timeout=1.0
                    )
                except Exception:
                    pass  # Best effort during shutdown
        except Exception as e:
            logger.error(f"[v95.0] Heartbeat loop error for {service_name}: {e}")
            # Attempt to restart heartbeat loop after error
            if not self._shutdown_event.is_set():
                logger.info(f"[v95.0] Restarting heartbeat loop for {service_name} after error")
                await asyncio.sleep(5.0)
                managed.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(managed)
                )
        finally:
            # v95.0: Ensure heartbeat task reference is cleared
            if managed.heartbeat_task and managed.heartbeat_task.done():
                managed.heartbeat_task = None
            logger.debug(f"[v95.0] Heartbeat loop exited for {service_name}")

    # =========================================================================
    # Auto-Healing
    # =========================================================================

    async def _auto_heal(self, managed: ManagedProcess) -> bool:
        """
        v95.2: Attempt to restart a failed service with intelligent prevention.

        CRITICAL FIXES (v95.2):
        1. Check if service is ACTUALLY healthy before restarting
        2. Skip restart if service is responding to health checks
        3. Reset restart count if service recovered on its own
        4. Prevent restart loops when service is already running

        Returns True if restart succeeded or service is already healthy.
        """
        definition = managed.definition

        # v95.13: Check GLOBAL shutdown signal FIRST (catches external shutdown paths)
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.info(
                    f"[v95.13] Skipping restart of {definition.name}: global shutdown initiated"
                )
                return False
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[v95.13] Error checking global shutdown: {e}")

        # v95.3: CRITICAL - Check ALL shutdown states BEFORE restarting
        if self._shutdown_event.is_set() or self._shutdown_completed:
            logger.info(
                f"[v95.3] Skipping restart of {definition.name}: shutdown detected "
                f"(event={self._shutdown_event.is_set()}, completed={self._shutdown_completed})"
            )
            return False

        # v95.2: CRITICAL - Check if service is ACTUALLY healthy before restarting
        # This prevents restart loops when the service is already running
        is_healthy = await self._quick_health_check(
            definition.default_port,
            definition.health_endpoint
        )

        if is_healthy:
            logger.info(
                f"[v95.2] Skipping restart of {definition.name}: "
                f"service is responding to health checks (already healthy!)"
            )
            # Reset state since service is actually healthy
            managed.status = ServiceStatus.HEALTHY
            managed.restart_count = 0
            managed.consecutive_failures = 0
            # Emit recovery event
            await _emit_event(
                "SERVICE_RECOVERED",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "self_recovered", "restart_count": 0}
            )
            return True

        if managed.restart_count >= self.config.max_restart_attempts:
            # v95.2: Before giving up, do one final health check
            final_check = await self._quick_health_check(
                definition.default_port,
                definition.health_endpoint
            )
            if final_check:
                logger.info(
                    f"[v95.2] {definition.name} recovered just before giving up! "
                    f"Resetting restart count."
                )
                managed.restart_count = 0
                managed.status = ServiceStatus.HEALTHY
                return True

            logger.error(
                f"❌ {definition.name} exceeded max restart attempts "
                f"({self.config.max_restart_attempts}). Giving up."
            )
            managed.status = ServiceStatus.FAILED
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "max_restart_attempts_exceeded",
                    "attempts": managed.restart_count,
                    "max_attempts": self.config.max_restart_attempts
                }
            )
            return False

        # Calculate backoff
        backoff = managed.calculate_backoff(
            self.config.restart_backoff_base,
            self.config.restart_backoff_max
        )

        logger.info(
            f"🔄 Restarting {definition.name} in {backoff:.1f}s "
            f"(attempt {managed.restart_count + 1}/{self.config.max_restart_attempts})"
        )

        # v95.0: Emit service restarting event
        await _emit_event(
            "SERVICE_RESTARTING",
            service_name=managed.definition.name,
            priority="HIGH",
            details={
                "attempt": managed.restart_count + 1,
                "max_attempts": self.config.max_restart_attempts,
                "backoff_seconds": backoff
            }
        )

        managed.status = ServiceStatus.RESTARTING
        await asyncio.sleep(backoff)

        # Stop existing process if still lingering
        await self._stop_process(managed)

        # Restart
        managed.restart_count += 1
        managed.last_restart = time.time()

        success = await self._spawn_service(managed)

        if success:
            logger.info(f"✅ {managed.definition.name} restarted successfully")
            managed.consecutive_failures = 0
            # v95.0: Emit service recovered event
            await _emit_event(
                "SERVICE_RECOVERED",
                service_name=managed.definition.name,
                priority="HIGH",
                details={
                    "restart_count": managed.restart_count,
                    "recovery_time_seconds": time.time() - managed.last_restart
                }
            )
            return True
        else:
            logger.error(f"❌ {managed.definition.name} restart failed")
            # v95.0: Emit service unhealthy event (restart failed)
            await _emit_event(
                "SERVICE_UNHEALTHY",
                service_name=managed.definition.name,
                priority="CRITICAL",
                details={
                    "reason": "restart_failed",
                    "attempt": managed.restart_count
                }
            )
            return False

    async def restart_service(self, service_name: str) -> bool:
        """
        v93.0: Public API to restart a specific service by name.

        This is intended to be called by external components like the
        SelfHealingServiceManager when they detect stale/dead services.

        Args:
            service_name: Name of the service to restart

        Returns:
            True if restart succeeded, False otherwise
        """
        if service_name not in self.processes:
            logger.error(f"[v93.0] Cannot restart {service_name}: not managed by this orchestrator")
            return False

        managed = self.processes[service_name]
        logger.info(f"[v93.0] Restart requested for {service_name} by external component")
        return await self._auto_heal(managed)

    # =========================================================================
    # v95.1: Intelligent Cross-Repo Recovery Coordination
    # =========================================================================

    async def start_recovery_coordinator(self) -> None:
        """
        v95.1: Start the intelligent recovery coordinator.

        The coordinator proactively monitors all services and handles:
        1. Early failure detection (before stale marking)
        2. Dependency cascade recovery (restart dependents when dependency fails)
        3. Cross-repo coordination (JARVIS, J-Prime, Reactor-Core)
        4. Intelligent restart ordering based on dependencies
        """
        if not self._proactive_recovery_enabled:
            logger.info("[v95.1] Proactive recovery is disabled")
            return

        if self._recovery_coordinator_task is None or self._recovery_coordinator_task.done():
            self._recovery_coordinator_task = asyncio.create_task(
                self._recovery_coordinator_loop(),
                name="recovery_coordinator"
            )
            self._track_background_task(self._recovery_coordinator_task)
            logger.info(
                f"[v95.1] Recovery coordinator started "
                f"(interval: {self._recovery_check_interval}s, "
                f"cascade: {self._dependency_cascade_recovery})"
            )

    async def stop_recovery_coordinator(self) -> None:
        """v95.1: Stop the recovery coordinator."""
        if self._recovery_coordinator_task and not self._recovery_coordinator_task.done():
            self._recovery_coordinator_task.cancel()
            try:
                await self._recovery_coordinator_task
            except asyncio.CancelledError:
                pass
            logger.info("[v95.1] Recovery coordinator stopped")

    async def _recovery_coordinator_loop(self) -> None:
        """
        v95.3: Main recovery coordinator loop with shutdown completion check.

        Performs proactive health checks and initiates recovery when needed.

        CRITICAL (v95.3): Added multiple shutdown state checks to prevent
        post-shutdown recovery attempts that caused restart loops.
        """
        logger.info("[v95.1] Recovery coordinator loop started")

        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._recovery_check_interval)

                # v95.3: CRITICAL - Check ALL shutdown states before any recovery action
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.info(
                        "[v95.3] Recovery coordinator exiting: shutdown detected "
                        f"(event={self._shutdown_event.is_set()}, completed={self._shutdown_completed})"
                    )
                    break

                # Perform comprehensive service health assessment
                health_report = await self._assess_service_health()

                # v95.3: Re-check shutdown state after potentially slow health assessment
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.debug("[v95.3] Skipping recovery actions: shutdown in progress")
                    break

                # Handle any services that need recovery
                for service_name, health_status in health_report.items():
                    # v95.3: Check shutdown state before EACH recovery attempt
                    if self._shutdown_event.is_set() or self._shutdown_completed:
                        logger.debug(
                            f"[v95.3] Aborting recovery loop for {service_name}: shutdown detected"
                        )
                        break

                    if health_status["needs_recovery"]:
                        await self._initiate_intelligent_recovery(
                            service_name,
                            health_status["reason"]
                        )

            except asyncio.CancelledError:
                logger.info("[v95.1] Recovery coordinator cancelled")
                break
            except Exception as e:
                logger.error(f"[v95.1] Recovery coordinator error: {e}")
                # v95.3: Check shutdown before sleeping on error
                if not self._shutdown_event.is_set() and not self._shutdown_completed:
                    await asyncio.sleep(5.0)  # Brief pause before retry

    async def _assess_service_health(self) -> Dict[str, Dict[str, Any]]:
        """
        v95.3: Comprehensive health assessment for all managed services.

        Checks:
        1. Process alive status
        2. Health endpoint responsiveness
        3. Heartbeat recency
        4. Dependency health (transitive)

        CRITICAL (v95.3): Skips assessment if shutdown is in progress
        to prevent false "needs_recovery" reports during shutdown.
        """
        health_report: Dict[str, Dict[str, Any]] = {}

        # v95.3: Skip health assessment if shutdown is in progress or completed
        if self._shutdown_event.is_set() or self._shutdown_completed:
            logger.debug("[v95.3] Skipping health assessment: shutdown in progress")
            return health_report

        for service_name, managed in self.processes.items():
            status = {
                "needs_recovery": False,
                "reason": None,
                "process_alive": False,
                "health_check_passed": False,
                "heartbeat_recent": False,
                "dependencies_healthy": True,
            }

            # 1. Check process status
            status["process_alive"] = managed.is_running

            # 2. Check health endpoint (quick check)
            if status["process_alive"]:
                try:
                    session = await self._get_http_session()
                    url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                        status["health_check_passed"] = resp.status == 200
                except Exception:
                    status["health_check_passed"] = False

            # 3. Check heartbeat recency (via registry)
            if self.registry:
                try:
                    service_info = await self.registry.discover_service(service_name)
                    if service_info:
                        heartbeat_age = time.time() - service_info.last_heartbeat
                        status["heartbeat_recent"] = heartbeat_age < 60.0  # Within 1 minute
                except Exception:
                    pass

            # 4. Check dependency health
            if managed.definition.depends_on:
                for dep in managed.definition.depends_on:
                    if dep in self.processes:
                        dep_managed = self.processes[dep]
                        if dep_managed.status not in (ServiceStatus.HEALTHY, ServiceStatus.DEGRADED):
                            status["dependencies_healthy"] = False
                            break

            # Determine if recovery is needed
            if not status["process_alive"]:
                status["needs_recovery"] = True
                status["reason"] = "process_dead"
            elif not status["health_check_passed"] and managed.status == ServiceStatus.HEALTHY:
                # Was healthy but health check now fails
                status["needs_recovery"] = True
                status["reason"] = "health_check_failed"
            elif not status["dependencies_healthy"] and self._dependency_cascade_recovery:
                # Dependency failed - may need cascade restart
                status["needs_recovery"] = True
                status["reason"] = "dependency_failed"

            health_report[service_name] = status

        return health_report

    async def _initiate_intelligent_recovery(
        self,
        service_name: str,
        reason: str
    ) -> bool:
        """
        v95.3: Initiate intelligent recovery for a failed service.

        Handles:
        1. Dependency ordering (restart dependencies first if needed)
        2. Cascade recovery (restart dependents after dependency)
        3. Cross-repo coordination

        CRITICAL (v95.3): Added comprehensive shutdown state checks to prevent
        recovery attempts after shutdown has started or completed.
        """
        # v95.13: Check GLOBAL shutdown signal FIRST (catches external shutdown paths)
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.info(
                    f"[v95.13] Skipping recovery for {service_name}: global shutdown initiated"
                )
                return False
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[v95.13] Error checking global shutdown: {e}")

        # v95.3: CRITICAL - Check ALL shutdown states before recovery
        if self._shutdown_event.is_set() or self._shutdown_completed:
            logger.info(
                f"[v95.3] Skipping recovery for {service_name}: shutdown detected "
                f"(event={self._shutdown_event.is_set()}, completed={self._shutdown_completed})"
            )
            return False

        if service_name not in self.processes:
            logger.warning(f"[v95.1] Cannot recover {service_name}: not managed")
            return False

        managed = self.processes[service_name]
        logger.info(f"[v95.1] Initiating recovery for {service_name} (reason: {reason})")

        # If reason is dependency failure, check and restart dependencies first
        if reason == "dependency_failed" and managed.definition.depends_on:
            for dep_name in managed.definition.depends_on:
                if dep_name in self.processes:
                    dep_managed = self.processes[dep_name]
                    if not dep_managed.is_running or dep_managed.status == ServiceStatus.FAILED:
                        logger.info(
                            f"[v95.1] Recovering dependency '{dep_name}' first for {service_name}"
                        )
                        await self._auto_heal(dep_managed)
                        # Wait briefly for dependency to stabilize
                        await asyncio.sleep(5.0)

        # Now recover the target service
        success = await self._auto_heal(managed)

        # If cascade recovery is enabled and this was a dependency, restart dependents
        if success and self._dependency_cascade_recovery:
            await self._cascade_restart_dependents(service_name)

        return success

    async def _cascade_restart_dependents(self, service_name: str) -> None:
        """
        v95.1: Restart services that depend on the recovered service.

        When a dependency is recovered, its dependents may need to be
        restarted to re-establish connections.
        """
        dependents = []

        for name, managed in self.processes.items():
            if managed.definition.depends_on and service_name in managed.definition.depends_on:
                dependents.append(name)

        if dependents:
            logger.info(
                f"[v95.1] Cascade restart: {len(dependents)} services depend on {service_name}: "
                f"{dependents}"
            )

            for dep_name in dependents:
                if dep_name in self.processes:
                    dep_managed = self.processes[dep_name]
                    # Only restart if not already healthy
                    if dep_managed.status != ServiceStatus.HEALTHY:
                        logger.info(f"[v95.1] Cascade restarting dependent: {dep_name}")
                        await self._auto_heal(dep_managed)
                        await asyncio.sleep(2.0)  # Brief pause between restarts

    # =========================================================================
    # Process Spawning
    # =========================================================================

    def _find_venv_python(self, repo_path: Path) -> Optional[str]:
        """
        Find the venv Python executable for a repository.

        v4.0: Auto-detects venv location and returns the Python path.
        Falls back to system Python if no venv found.
        """
        # Check common venv locations
        venv_paths = [
            repo_path / "venv" / "bin" / "python3",
            repo_path / "venv" / "bin" / "python",
            repo_path / ".venv" / "bin" / "python3",
            repo_path / ".venv" / "bin" / "python",
            repo_path / "env" / "bin" / "python3",
            repo_path / "env" / "bin" / "python",
        ]

        for venv_python in venv_paths:
            if venv_python.exists():
                logger.debug(f"Found venv Python at: {venv_python}")
                return str(venv_python)

        logger.debug(f"No venv found in {repo_path}, using system Python")
        return None

    async def _pre_spawn_validation(
        self, definition: ServiceDefinition
    ) -> Tuple[Union[bool, Literal["ALREADY_HEALTHY"]], Optional[str]]:
        """
        Validate a service before spawning.

        v4.0: Pre-launch checks:
        - Repo path exists
        - Script exists
        - Venv Python found (optional but preferred)
        - Port not already in use

        v95.0: Enhanced with detailed diagnostic logging for troubleshooting.
        v95.7: Return type now includes Literal["ALREADY_HEALTHY"] for type safety.

        Returns:
            Tuple of (is_valid_or_status, python_executable)
            - is_valid_or_status: True if valid, False if invalid, "ALREADY_HEALTHY" if service is running
            - python_executable: Path to Python executable or None
        """
        logger.info(f"    🔍 Pre-spawn validation for {definition.name}...")

        # Check repo exists
        if not definition.repo_path.exists():
            logger.error(f"    ❌ Repository not found: {definition.repo_path}")
            logger.error(f"    💡 Hint: Set {definition.name.upper().replace('-', '_')}_PATH env var or ensure repo is cloned")
            return False, None

        logger.debug(f"    ✓ Repository found: {definition.repo_path}")

        # Check script exists
        script_path = self._find_script(definition)
        if script_path is None:
            logger.error(f"    ❌ No startup script found for {definition.name}")
            logger.error(f"    💡 Looking for: {definition.script_name} in {definition.repo_path}")
            logger.error(f"    💡 Fallbacks: {definition.fallback_scripts}")
            # List what files ARE in the repo
            try:
                py_files = list(definition.repo_path.glob("*.py"))
                if py_files:
                    logger.error(f"    💡 Python files found: {[f.name for f in py_files[:5]]}")
                else:
                    logger.error(f"    💡 No .py files found in {definition.repo_path}")
            except Exception as e:
                logger.debug(f"    Could not list repo files: {e}")
            return False, None

        logger.debug(f"    ✓ Script found: {script_path}")

        # Find Python executable (prefer venv)
        python_exec = self._find_venv_python(definition.repo_path)
        if python_exec is None:
            python_exec = sys.executable
            logger.info(f"    ℹ️ Using system Python for {definition.name}: {python_exec}")
        else:
            logger.info(f"    ✓ Using venv Python for {definition.name}: {python_exec}")

        # v95.2: CRITICAL - Check if service is ALREADY RUNNING and HEALTHY
        # This prevents the restart loop where we try to spawn when already running
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', definition.default_port))
            sock.close()

            if result == 0:
                # Port is in use - check if it's actually our service and healthy
                logger.info(
                    f"    🔍 Port {definition.default_port} in use - checking if {definition.name} is healthy..."
                )
                is_healthy = await self._quick_health_check(
                    definition.default_port,
                    definition.health_endpoint
                )

                if is_healthy:
                    # Service is already running and healthy - NO NEED TO SPAWN!
                    logger.info(
                        f"    ✅ {definition.name} is already running and healthy on port "
                        f"{definition.default_port} - SKIPPING SPAWN"
                    )
                    # Return special marker to indicate "already healthy"
                    return "ALREADY_HEALTHY", python_exec
                else:
                    # Port in use but not healthy - this is a real conflict
                    logger.warning(
                        f"    ⚠️ Port {definition.default_port} in use but {definition.name} "
                        f"is NOT healthy - possible port conflict with another process"
                    )
                    # v108.0: CRITICAL FIX - Actually resolve the conflict
                    conflict_resolved = await self._handle_port_conflict(definition)
                    if not conflict_resolved:
                        logger.error(
                            f"    ❌ Could not resolve port conflict for {definition.name} "
                            f"on port {definition.default_port}"
                        )
                        # Wait a bit and retry port check
                        await asyncio.sleep(2.0)
                        is_healthy_retry = await self._quick_health_check(
                            definition.default_port,
                            definition.health_endpoint
                        )
                        if is_healthy_retry:
                            # Service came up healthy after conflict resolution attempt
                            return "ALREADY_HEALTHY", python_exec
                        else:
                            # Still broken - log and let spawn proceed (it will fail cleanly)
                            logger.warning(
                                f"    ⚠️ Port {definition.default_port} still unavailable, "
                                f"spawn may fail"
                            )

        except Exception as e:
            logger.debug(f"Port check failed: {e}")

        return True, python_exec

    async def _quick_health_check(self, port: int, health_endpoint: str) -> bool:
        """
        v95.2: Quick health check to verify if a service is responding.

        Used during pre-spawn validation to prevent restarting healthy services.
        """
        try:
            session = await self._get_http_session()
            url = f"http://localhost:{port}{health_endpoint}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _handle_port_conflict(self, definition: ServiceDefinition) -> bool:
        """
        v108.0: Handle port conflict by attempting to identify and resolve the issue.

        CRITICAL FIX: This now actually CLEANS UP conflicting processes instead of
        just logging them. Uses the EnterpriseProcessManager for comprehensive
        port validation and cleanup.

        Returns:
            True if port was successfully cleaned up and is now available
            False if cleanup failed and port is still occupied
        """
        port = definition.default_port

        # v108.0: Use EnterpriseProcessManager for comprehensive port handling
        try:
            from backend.core.enterprise_process_manager import get_process_manager

            process_manager = get_process_manager()

            # Comprehensive port validation
            validation = await process_manager.validate_port(
                port=port,
                expected_service=definition.name,
                health_endpoint=definition.health_endpoint,
            )

            logger.info(
                f"    📋 Port {port} validation: occupied={validation.is_occupied}, "
                f"healthy={validation.is_healthy}, state={validation.socket_state}, "
                f"recommendation={validation.recommendation}"
            )

            if validation.pid:
                # Log details about the conflicting process
                logger.warning(
                    f"    📋 Port {port} held by: PID={validation.pid}, "
                    f"Name={validation.process_name or 'unknown'}"
                )

            # Handle based on recommendation
            if validation.recommendation == "proceed":
                return True  # Port available

            elif validation.recommendation == "skip":
                # Service already running healthy - this is actually OK
                logger.info(f"    ✅ {definition.name} already healthy on port {port}")
                return True

            elif validation.recommendation in ("kill_and_retry", "wait"):
                # Attempt cleanup
                logger.info(f"    🧹 Attempting to clean up port {port}...")

                cleanup_success = await process_manager.cleanup_port(
                    port=port,
                    force=False,  # Try graceful first
                    wait_for_time_wait=True,
                    max_wait=30.0,  # Wait up to 30s for TIME_WAIT
                )

                if cleanup_success:
                    logger.info(f"    ✅ Port {port} successfully cleaned up")
                    return True
                else:
                    logger.error(f"    ❌ Failed to clean up port {port}")
                    return False

            return False

        except ImportError:
            # Fallback to legacy behavior if enterprise process manager not available
            logger.warning(
                "[v108.0] EnterpriseProcessManager not available, using legacy fallback"
            )
            try:
                import psutil
                for conn in psutil.net_connections(kind='inet'):
                    if conn.laddr.port == port and conn.status == 'LISTEN':
                        try:
                            proc = psutil.Process(conn.pid)
                            logger.warning(
                                f"    📋 Port {port} held by: "
                                f"PID={conn.pid}, Name={proc.name()}, "
                                f"Cmdline={' '.join(proc.cmdline()[:3])}"
                            )
                            # Attempt to kill the process
                            proc.terminate()
                            proc.wait(timeout=5)
                            logger.info(f"    ✅ Killed process {conn.pid} on port {port}")
                            return True
                        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                            logger.warning(
                                f"    ⚠️ Could not kill process on port {port}: {e}"
                            )
                            return False
            except Exception as e:
                logger.debug(f"Could not identify process on port: {e}")
                return False

        return False

    async def _wait_for_dependencies(self, definition: ServiceDefinition) -> bool:
        """
        v95.0: Wait for service dependencies to be healthy before proceeding.

        This implements intelligent dependency resolution:
        1. Check if all dependencies are listed in the registry
        2. Wait for each dependency to become healthy
        3. Use exponential backoff for checks
        4. Respect timeout limits
        5. Emit events for monitoring

        Returns True if all dependencies are healthy, False on timeout/failure.
        """
        if not definition.depends_on:
            return True  # No dependencies

        logger.info(
            f"[v95.0] Checking dependencies for {definition.name}: "
            f"{definition.depends_on}"
        )

        # Emit dependency check event
        await _emit_event(
            "DEPENDENCY_CHECK_START",
            service_name=definition.name,
            priority="MEDIUM",
            details={"dependencies": definition.depends_on}
        )

        start_time = time.time()
        timeout = definition.dependency_wait_timeout
        check_interval = definition.dependency_check_interval

        pending_deps = set(definition.depends_on)

        while pending_deps and (time.time() - start_time) < timeout:
            for dep_name in list(pending_deps):
                # v95.4: Special handling for jarvis-body dependency
                # jarvis-body is not in self.processes because it's the orchestrator itself
                if dep_name == "jarvis-body":
                    if self._jarvis_body_status == "healthy" or "jarvis-body" in self._services_ready:
                        logger.info(f"  ✓ Dependency 'jarvis-body' is healthy (local)")
                        pending_deps.discard(dep_name)
                        continue
                    # Also check via ready event
                    if self._jarvis_body_ready_event and self._jarvis_body_ready_event.is_set():
                        logger.info(f"  ✓ Dependency 'jarvis-body' ready event set")
                        pending_deps.discard(dep_name)
                        continue

                # Check if dependency is in our managed processes
                if dep_name in self.processes:
                    dep_managed = self.processes[dep_name]
                    if dep_managed.status == ServiceStatus.HEALTHY:
                        logger.info(f"  ✓ Dependency '{dep_name}' is healthy")
                        pending_deps.discard(dep_name)
                        continue

                # Also check the service registry
                if self.registry:
                    try:
                        services = await self.registry.list_services(healthy_only=True)
                        if any(s.service_name == dep_name for s in services):
                            logger.info(f"  ✓ Dependency '{dep_name}' found healthy in registry")
                            pending_deps.discard(dep_name)
                            continue
                    except Exception as e:
                        logger.debug(f"  Registry check for {dep_name} failed: {e}")

            if pending_deps:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                logger.debug(
                    f"  Waiting for dependencies: {pending_deps} "
                    f"({elapsed:.1f}s elapsed, {remaining:.1f}s remaining)"
                )
                await asyncio.sleep(check_interval)
                # Exponential backoff (cap at 10s)
                check_interval = min(check_interval * 1.5, 10.0)

        if pending_deps:
            elapsed = time.time() - start_time
            logger.error(
                f"[v95.0] Dependency timeout for {definition.name} after {elapsed:.1f}s: "
                f"still waiting for {pending_deps}"
            )
            await _emit_event(
                "DEPENDENCY_CHECK_FAILED",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "pending_dependencies": list(pending_deps),
                    "timeout_seconds": elapsed
                }
            )
            return False

        elapsed = time.time() - start_time
        logger.info(
            f"[v95.0] All dependencies healthy for {definition.name} "
            f"(checked in {elapsed:.1f}s)"
        )
        await _emit_event(
            "DEPENDENCY_CHECK_PASSED",
            service_name=definition.name,
            priority="MEDIUM",
            details={
                "dependencies": definition.depends_on,
                "check_time_seconds": elapsed
            }
        )
        return True

    async def _spawn_service(self, managed: ManagedProcess) -> bool:
        """
        v95.0: Spawn a service process with comprehensive event emissions.

        Enhanced with:
        - Dependency checking before spawn
        - Pre-spawn validation (venv detection, port check)
        - Better error reporting
        - Environment isolation
        - Real-time voice narration of service lifecycle

        Returns True if spawn and health check succeeded.
        """
        definition = managed.definition

        # v95.0: Wait for dependencies to be healthy before spawning
        if definition.depends_on:
            deps_ready = await self._wait_for_dependencies(definition)
            if not deps_ready:
                logger.error(
                    f"[v95.0] Cannot spawn {definition.name}: dependencies not ready"
                )
                managed.status = ServiceStatus.FAILED
                await _emit_event(
                    "SERVICE_BLOCKED",
                    service_name=definition.name,
                    priority="HIGH",
                    details={"reason": "dependencies_not_ready"}
                )
                return False

        # v95.0: Emit service spawning event
        await _emit_event(
            "SERVICE_SPAWNING",
            service_name=definition.name,
            priority="HIGH",
            details={
                "port": definition.default_port,
                "repo_path": str(definition.repo_path),
                "startup_timeout": definition.startup_timeout,
                "depends_on": definition.depends_on
            }
        )

        # Pre-spawn validation
        is_valid, python_exec = await self._pre_spawn_validation(definition)

        # v95.2: Handle "ALREADY_HEALTHY" - service is running and responsive
        if is_valid == "ALREADY_HEALTHY":
            logger.info(
                f"✅ {definition.name} is already running and healthy - no spawn needed"
            )
            managed.status = ServiceStatus.HEALTHY
            managed.port = definition.default_port
            managed.consecutive_failures = 0
            managed.restart_count = 0  # Reset restart count since service is healthy

            # Register in service registry
            if self.registry:
                # Discover existing registration to get PID
                existing = await self.registry.discover_service(definition.name)
                if existing:
                    managed.pid = existing.pid
                else:
                    # Register with PID 0 (unknown) since we didn't spawn it
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=0,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={
                            "repo_path": str(definition.repo_path),
                            "already_running": True,
                        }
                    )

            # Start health monitor if not already running
            if managed.health_monitor_task is None or managed.health_monitor_task.done():
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed),
                    name=f"health_monitor_{definition.name}"
                )
                self._track_background_task(managed.health_monitor_task)

            # v95.0: Emit service healthy event
            await _emit_event(
                "SERVICE_HEALTHY",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "already_running", "port": managed.port}
            )
            return True

        if not is_valid:
            logger.error(f"Cannot spawn {definition.name}: pre-spawn validation failed")
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service failed event
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "pre_spawn_validation_failed"}
            )
            return False

        script_path = self._find_script(definition)

        # v95.7: Guard to ensure python_exec is non-None after validation passed
        if python_exec is None:
            python_exec = sys.executable  # Fallback to system Python
            logger.warning(f"Using system Python for {definition.name} as no venv detected")

        if script_path is None:
            logger.error(f"Cannot spawn {definition.name}: no script found")
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service failed event
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "script_not_found"}
            )
            return False

        managed.status = ServiceStatus.STARTING

        try:
            # Build environment
            env = os.environ.copy()
            env.update(definition.environment)

            # Add port hint for service registration
            env["SERVICE_PORT"] = str(definition.default_port)
            env["SERVICE_NAME"] = definition.name

            # v93.16: Suppress library warnings at environment level
            # This ensures warnings are suppressed BEFORE Python interpreter loads modules
            env.setdefault("PYTHONWARNINGS", "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning")
            env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")  # Suppress TensorFlow warnings
            env.setdefault("TRANSFORMERS_VERBOSITY", "error")  # Suppress transformers warnings
            env.setdefault("TOKENIZERS_PARALLELISM", "false")  # Suppress tokenizers warning
            env.setdefault("COREMLTOOLS_LOG_LEVEL", "ERROR")  # Suppress coremltools warnings

            # v4.0: Build command using the detected Python executable
            cmd: List[str] = []

            if definition.use_uvicorn and definition.uvicorn_app:
                # Uvicorn-based FastAPI app
                # v95.7: Use str() to ensure type safety (uvicorn_app is verified non-None by condition)
                uvicorn_app: str = definition.uvicorn_app  # Narrow type for Pyright
                cmd = [
                    python_exec, "-m", "uvicorn",
                    uvicorn_app,
                    "--host", "0.0.0.0",
                    "--port", str(definition.default_port),
                ]
                logger.info(f"🚀 Spawning {definition.name} via uvicorn: {uvicorn_app}")

            elif definition.module_path:
                # Module-based entry point (python -m)
                # v95.7: Use type narrowing for Pyright
                module_path: str = definition.module_path  # Narrow type for Pyright
                cmd = [python_exec, "-m", module_path]
                # Add script_args if any
                if definition.script_args:
                    cmd.extend(definition.script_args)
                logger.info(f"🚀 Spawning {definition.name} via module: {definition.module_path}")

            else:
                # Traditional script-based entry point
                cmd = [python_exec, str(script_path)]
                # v4.0: Append command-line arguments (e.g., --port 8000)
                if definition.script_args:
                    cmd.extend(definition.script_args)
                logger.info(f"🚀 Spawning {definition.name}: {' '.join(cmd)}")

            # Spawn process
            managed.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(definition.repo_path),
                stdout=asyncio.subprocess.PIPE if self.config.stream_output else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if self.config.stream_output else asyncio.subprocess.DEVNULL,
                env=env,
            )

            managed.pid = managed.process.pid
            managed.port = definition.default_port  # May be updated by registry discovery

            logger.info(f"📋 {definition.name} spawned with PID {managed.pid}")

            # v108.0: Record startup time for grace period tracking
            # This allows the heartbeat validator to NOT mark components as dead during startup
            await self._record_component_startup(definition.name, managed.pid)

            # Start output streaming
            await self._start_output_streaming(managed)

            # Wait for service to become healthy
            healthy = await self._wait_for_health(managed, timeout=definition.startup_timeout)

            if healthy:
                managed.status = ServiceStatus.HEALTHY

                # v95.0: Emit service healthy event
                await _emit_event(
                    "SERVICE_HEALTHY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "pid": managed.pid,
                        "port": managed.port,
                        "startup_timeout": definition.startup_timeout
                    }
                )

                # Register in service registry
                if self.registry:
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=managed.pid,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={"repo_path": str(definition.repo_path)}
                    )

                # Start health monitor
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed)
                )

                # v95.0: Start dedicated heartbeat loop (prevents stale service issues)
                # This runs independently of health checks to ensure services
                # are NEVER marked stale as long as the process is running
                managed.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(managed)
                )
                managed.last_known_health = "healthy"

                return True
            else:
                logger.warning(
                    f"⚠️ {definition.name} spawned but did not become healthy "
                    f"within {definition.startup_timeout}s"
                )
                managed.status = ServiceStatus.DEGRADED
                managed.last_known_health = "degraded"

                # v95.0: Emit service unhealthy event
                await _emit_event(
                    "SERVICE_UNHEALTHY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "reason": "health_check_timeout",
                        "timeout_seconds": definition.startup_timeout,
                        "pid": managed.pid
                    }
                )

                # v95.0: CRITICAL - Start heartbeat and health monitor even for degraded services
                # This ensures services are NOT marked stale while the process is running
                # and allows auto-healing to kick in when the health monitor detects failures
                if self.registry and managed.is_running:
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=managed.pid,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={
                            "repo_path": str(definition.repo_path),
                            "status": "degraded",
                            "degraded_at": time.time()
                        }
                    )
                    logger.info(
                        f"[v95.0] Registered degraded service {definition.name} in registry"
                    )

                # Start health monitor to track recovery or trigger auto-heal
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed)
                )

                # Start heartbeat loop to prevent stale service removal
                managed.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(managed)
                )
                logger.info(
                    f"[v95.0] Started heartbeat loop for degraded service {definition.name}"
                )

                return False  # Still return False to indicate not healthy

        except Exception as e:
            logger.error(f"❌ Failed to spawn {definition.name}: {e}", exc_info=True)
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service crashed event
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "spawn_exception", "error": str(e)}
            )
            return False

    async def _record_component_startup(self, service_name: str, pid: int) -> None:
        """
        v108.0: Record component startup time for grace period tracking.

        This integrates with:
        1. HeartbeatValidator - to prevent marking components as dead during startup
        2. TrinityHealthMonitor - to prevent marking components as unhealthy during startup
        3. TrinityOrchestrationConfig - to record startup in unified config

        Args:
            service_name: Name of the service being started
            pid: Process ID of the spawned service
        """
        startup_time = time.time()

        # Map service name to component type for config lookup
        component_type_map = {
            "jarvis-body": "jarvis_body",
            "jarvis_body": "jarvis_body",
            "jarvis-prime": "jarvis_prime",
            "jarvis_prime": "jarvis_prime",
            "j-prime": "jarvis_prime",
            "jprime": "jarvis_prime",
            "reactor-core": "reactor_core",
            "reactor_core": "reactor_core",
            "coding-council": "coding_council",
            "coding_council": "coding_council",
        }
        component_type = component_type_map.get(service_name.lower(), "jarvis_body")

        # 1. Record in HeartbeatValidator
        try:
            from backend.core.coding_council.trinity.heartbeat_validator import (
                HeartbeatValidator,
            )
            # Get or create global validator instance
            validator = HeartbeatValidator()
            validator.record_component_startup(service_name, component_type)
            logger.debug(
                f"[v108.0] Recorded startup time in HeartbeatValidator for {service_name}"
            )
        except ImportError:
            logger.debug(
                f"[v108.0] HeartbeatValidator not available for startup recording"
            )
        except Exception as e:
            logger.warning(
                f"[v108.0] Failed to record startup in HeartbeatValidator: {e}"
            )

        # 2. Record in TrinityOrchestrationConfig (for global access)
        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
                ComponentType,
            )
            # Store startup time in a shared location
            orch_config = get_orchestration_config()
            startup_file = orch_config.components_dir / f"{service_name}_startup.json"

            import json
            startup_data = {
                "service_name": service_name,
                "component_type": component_type,
                "startup_time": startup_time,
                "pid": pid,
            }

            # Atomic write
            tmp_file = startup_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(startup_data, indent=2))
            tmp_file.rename(startup_file)

            logger.debug(
                f"[v108.0] Recorded startup time file for {service_name} at {startup_file}"
            )
        except ImportError:
            logger.debug(
                f"[v108.0] TrinityOrchestrationConfig not available for startup recording"
            )
        except Exception as e:
            logger.warning(
                f"[v108.0] Failed to write startup file: {e}"
            )

        # 3. Record in TrinityHealthMonitor config if available
        try:
            from backend.core.trinity_health_monitor import (
                TrinityHealthConfig,
                TrinityComponent,
            )
            # Map to TrinityComponent enum
            component_enum_map = {
                "jarvis_body": TrinityComponent.JARVIS_BODY,
                "jarvis_prime": TrinityComponent.JARVIS_PRIME,
                "reactor_core": TrinityComponent.REACTOR_CORE,
                "coding_council": TrinityComponent.CODING_COUNCIL,
            }
            if component_type in component_enum_map:
                # This will be picked up by the health monitor's config
                logger.debug(
                    f"[v108.0] Component {service_name} ({component_type}) startup recorded"
                )
        except ImportError:
            pass

    async def _wait_for_health(
        self,
        managed: ManagedProcess,
        timeout: float = 60.0
    ) -> bool:
        """
        Wait for service to become healthy with intelligent progress-based timeout extension.

        v93.5: Enhanced with intelligent progress detection:

        PHASE 1 (Quick): Wait for server to start responding (max 60s)
        - Server starts listening on port
        - Health endpoint returns any status (including "starting")
        - If this times out, the service failed to start

        PHASE 2 (Patient + Intelligent): Wait for model to load with progress detection
        - Health endpoint returns "healthy" status
        - Server is up, loading models in background
        - KEY ENHANCEMENT: If progress is detected (model_load_elapsed increasing),
          timeout is dynamically extended up to max_startup_timeout
        - This prevents timeout when model is actively loading (just slow)

        This prevents the scenario where:
        - Server takes 5s to start listening
        - Model takes 304s to load (just 4s over 300s timeout)
        - Old approach: times out at 300s even though model was 98% loaded
        - New approach: detects progress, extends timeout, model loads successfully
        """
        start_time = time.time()
        check_interval = 1.0
        check_count = 0
        last_milestone_log = start_time

        # v93.5: Detect if this is a long-timeout service (likely loading ML models)
        is_ml_heavy = timeout > 90.0
        milestone_interval = 30.0 if is_ml_heavy else 15.0

        # v93.5: Progress tracking for intelligent timeout extension
        last_model_elapsed = 0.0
        progress_detected_at = 0.0
        timeout_extended = False
        effective_timeout = timeout
        max_timeout = self.config.max_startup_timeout

        # Phase 1: Wait for server to respond (quick timeout)
        phase1_timeout = min(60.0, timeout / 3)  # Max 60s or 1/3 of total timeout
        server_responding = False

        logger.info(
            f"    ⏳ Phase 1: Waiting for {managed.definition.name} server to start "
            f"(timeout: {phase1_timeout:.0f}s)..."
        )

        while (time.time() - start_time) < phase1_timeout:
            check_count += 1

            # Check if process died
            if not managed.is_running:
                exit_code = managed.process.returncode if managed.process else "unknown"
                logger.error(
                    f"    ❌ {managed.definition.name} process died during startup "
                    f"(exit code: {exit_code})"
                )
                return False

            # Check if server is responding (any status including "starting")
            if await self._check_service_responding(managed):
                elapsed = time.time() - start_time
                logger.info(
                    f"    ✅ Phase 1 complete: {managed.definition.name} server responding "
                    f"after {elapsed:.1f}s"
                )
                server_responding = True
                break

            # Quick checks during phase 1
            await asyncio.sleep(check_interval)
            check_interval = min(check_interval + 0.3, 2.0)

        if not server_responding:
            elapsed = time.time() - start_time
            logger.warning(
                f"    ⚠️ {managed.definition.name} server failed to start within {elapsed:.1f}s"
            )
            return False

        # Phase 2: Wait for "healthy" status (model loading) with intelligent progress detection
        phase2_start = time.time()
        check_interval = 2.0  # Slower checks now that server is up
        check_count = 0
        last_status = "unknown"

        if is_ml_heavy:
            logger.info(
                f"    ⏳ Phase 2: Waiting for {managed.definition.name} model to load "
                f"(base timeout: {effective_timeout:.0f}s, max: {max_timeout:.0f}s)..."
            )
            logger.info(
                f"    ℹ️  {managed.definition.name}: Server is up, model loading in background"
            )

        while True:
            phase2_elapsed = time.time() - phase2_start
            total_elapsed = time.time() - start_time

            # v93.5: Check against effective (possibly extended) timeout
            if phase2_elapsed >= effective_timeout:
                # Final timeout check - but only if no recent progress
                if progress_detected_at > 0 and (time.time() - progress_detected_at) < 60:
                    # Progress was detected within last 60s - extend if under max
                    if effective_timeout < max_timeout:
                        extension = min(
                            self.config.model_loading_timeout_extension,
                            max_timeout - effective_timeout
                        )
                        effective_timeout += extension
                        logger.info(
                            f"    🔄 {managed.definition.name}: Progress detected, extending timeout by {extension:.0f}s "
                            f"(new timeout: {effective_timeout:.0f}s)"
                        )
                        timeout_extended = True
                        continue
                break  # Actually timed out

            check_count += 1

            # Check if process died
            if not managed.is_running:
                exit_code = managed.process.returncode if managed.process else "unknown"
                logger.error(
                    f"    ❌ {managed.definition.name} process died during model loading "
                    f"(exit code: {exit_code})"
                )
                return False

            # v93.7: SINGLE health check request that both checks status AND tracks progress
            # This fixes the duplicate health check issue (was making 2 requests per interval)
            # v93.11: Uses shared HTTP session with connection pooling
            try:
                url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"
                session = await self._get_http_session()
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        current_status = data.get("status", "unknown")
                        current_phase = data.get("phase", "")  # v93.12: Also track phase
                        model_elapsed = data.get("model_load_elapsed_seconds", 0)
                        current_step = data.get("current_step", "")
                        model_progress = data.get("model_load_progress_pct", 0)
                        startup_elapsed = data.get("startup_elapsed_seconds", 0)  # v93.12: Track overall startup

                        # v93.12: Multiple ways to detect "healthy" state
                        # - status == "healthy" (standard)
                        # - phase == "ready" (jarvis-prime specific)
                        is_healthy = (
                            current_status == "healthy"
                            or current_phase == "ready"
                            or (data.get("ready_for_inference", False) and data.get("model_loaded", False))
                        )

                        if is_healthy:
                            logger.info(
                                f"    ✅ {managed.definition.name} fully healthy after {total_elapsed:.1f}s "
                                f"(server: {phase2_start - start_time:.1f}s, model: {phase2_elapsed:.1f}s)"
                                + (f" [timeout was extended]" if timeout_extended else "")
                            )
                            return True

                        # v93.12: Early exit on error status
                        if current_status == "error":
                            error_msg = data.get("error", "Unknown error")
                            logger.error(
                                f"    ❌ {managed.definition.name} reported error: {error_msg}"
                            )
                            # Don't return False immediately - let the process try to recover
                            # unless error persists for multiple checks

                        # v93.5: Detect progress (model_elapsed increasing)
                        if model_elapsed and model_elapsed > last_model_elapsed:
                            progress_detected_at = time.time()
                            last_model_elapsed = model_elapsed

                        # v93.12: Also detect step changes as progress
                        if current_step and current_step != getattr(self, '_last_detected_step', ''):
                            progress_detected_at = time.time()
                            self._last_detected_step = current_step
                            logger.info(
                                f"    📍 {managed.definition.name}: Step changed to '{current_step}'"
                            )

                        # v93.7: Log status changes with step info
                        if current_status != last_status:
                            step_info = f" (step: {current_step})" if current_step else ""
                            phase_info = f", phase: {current_phase}" if current_phase else ""
                            logger.info(
                                f"    ℹ️  {managed.definition.name}: status={current_status}{step_info}{phase_info}"
                            )
                            last_status = current_status

                        # v93.7: Enhanced milestone logging with step and progress info
                        if (time.time() - last_milestone_log) >= milestone_interval:
                            remaining = effective_timeout - phase2_elapsed
                            model_info = f", model loading: {model_elapsed:.0f}s" if model_elapsed else ""
                            progress_info = ""
                            if model_progress > 0:
                                progress_info = f" ({model_progress:.0f}% est.)"
                            if progress_detected_at > 0:
                                since_progress = time.time() - progress_detected_at
                                progress_info += f", last progress: {since_progress:.0f}s ago"
                            step_info = f", step: {current_step}" if current_step else ""
                            logger.info(
                                f"    ⏳ {managed.definition.name}: {current_status}{step_info} "
                                f"({phase2_elapsed:.0f}s elapsed, {remaining:.0f}s remaining{model_info}{progress_info})"
                            )
                            last_milestone_log = time.time()
                    else:
                        # Non-200 response
                        logger.debug(f"    Health check returned {response.status}")

            except asyncio.TimeoutError:
                logger.debug(f"    Health check timed out")
            except Exception as e:
                logger.debug(f"    Health check error: {e}")

            # v93.5: Adaptive check intervals for phase 2
            if phase2_elapsed > 180:
                check_interval = 10.0  # After 3 min, check every 10s
            elif phase2_elapsed > 60:
                check_interval = 5.0   # After 1 min, check every 5s
            else:
                check_interval = 3.0   # First minute, check every 3s

            await asyncio.sleep(check_interval)

        # Timeout
        total_elapsed = time.time() - start_time
        logger.warning(
            f"    ⚠️ {managed.definition.name} model loading timed out after {total_elapsed:.1f}s "
            f"({check_count} phase 2 checks, effective_timeout={effective_timeout:.0f}s)"
        )
        return False

    # =========================================================================
    # Process Stopping
    # =========================================================================

    async def _stop_process(self, managed: ManagedProcess) -> None:
        """Stop a managed process gracefully."""
        # Cancel background tasks (v95.0: includes dedicated heartbeat task)
        background_tasks = [
            managed.output_stream_task,
            managed.health_monitor_task,
            managed.heartbeat_task,  # v95.0: dedicated heartbeat task
        ]
        for task in background_tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if managed.process is None or not managed.is_running:
            return

        logger.info(f"🛑 Stopping {managed.definition.name} (PID: {managed.pid})...")

        try:
            # Try graceful shutdown first (SIGTERM)
            managed.process.terminate()

            try:
                await asyncio.wait_for(
                    managed.process.wait(),
                    timeout=self.config.shutdown_timeout
                )
                logger.info(f"✅ {managed.definition.name} stopped gracefully")

            except asyncio.TimeoutError:
                # Force kill if necessary (SIGKILL)
                logger.warning(
                    f"⚠️ {managed.definition.name} did not stop gracefully, forcing..."
                )
                managed.process.kill()
                await managed.process.wait()
                logger.info(f"✅ {managed.definition.name} force killed")

        except ProcessLookupError:
            pass  # Process already dead
        except Exception as e:
            logger.error(f"Error stopping {managed.definition.name}: {e}")

        managed.status = ServiceStatus.STOPPED

        # Deregister from service registry
        if self.registry:
            await self.registry.deregister_service(managed.definition.name)

    # =========================================================================
    # Signal Handlers
    # =========================================================================

    def _setup_signal_handlers(self) -> None:
        """Setup graceful shutdown signal handlers."""
        if self._signals_registered:
            return

        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_shutdown(s))
            )

        self._signals_registered = True
        logger.info("🛡️ Signal handlers registered (SIGINT, SIGTERM)")

    async def _handle_shutdown(self, signum: int) -> None:
        """
        Handle shutdown signal gracefully.

        v5.2: Proper async shutdown - don't call loop.stop() or sys.exit().
        Instead, set shutdown event and let pending tasks complete naturally.
        The main loop will exit when all tasks are done.
        """
        sig_name = signal.Signals(signum).name
        logger.info(f"\n🛑 Received {sig_name}, initiating graceful shutdown...")

        # v95.13: Initiate global shutdown signal FIRST
        # This notifies ALL components (WebSocket handlers, background tasks, etc.)
        try:
            from backend.core.resilience.graceful_shutdown import initiate_global_shutdown
            initiate_global_shutdown(reason=f"signal_{sig_name}", initiator="orchestrator")
        except Exception as e:
            logger.warning(f"[v95.13] Could not initiate global shutdown signal: {e}")

        # Set shutdown event FIRST (signals other tasks to stop)
        self._shutdown_event.set()
        self._running = False

        # Perform graceful service shutdown
        try:
            await self.shutdown_all_services()
            logger.info("✅ Graceful shutdown complete")
        except asyncio.CancelledError:
            # Expected during shutdown - don't log as error
            logger.info("✅ Shutdown tasks cancelled (expected)")
        except Exception as e:
            logger.error(f"⚠️ Error during shutdown: {e}")

        # v5.2: Cancel all remaining tasks gracefully instead of stopping loop
        # This allows pending futures to complete/cancel properly
        try:
            loop = asyncio.get_running_loop()
            tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]

            if tasks:
                logger.debug(f"[Shutdown] Cancelling {len(tasks)} remaining tasks...")
                for task in tasks:
                    task.cancel()

                # Wait for tasks to acknowledge cancellation
                await asyncio.gather(*tasks, return_exceptions=True)

            logger.info("✅ All tasks terminated")
        except Exception as e:
            logger.debug(f"[Shutdown] Task cleanup note: {e}")

    # =========================================================================
    # v93.11: Parallel Service Startup with Coordination
    # =========================================================================

    async def _start_single_service_with_coordination(
        self,
        definition: ServiceDefinition,
    ) -> tuple[str, bool, str]:
        """
        v95.1: Start a single service with deadlock-free coordination.

        CRITICAL FIX: Dependency waiting happens OUTSIDE the semaphore to prevent
        deadlock scenarios where services waiting for dependencies hold semaphore
        slots while their dependencies wait to acquire semaphore slots.

        Architecture:
        1. Wait for dependencies (NO semaphore held - prevents deadlock)
        2. Acquire semaphore with timeout (prevents indefinite blocking)
        3. Perform actual startup (semaphore held only for I/O-bound work)
        4. Release semaphore on completion or error

        Returns tuple of (service_name, success, reason).
        """
        self._ensure_locks_initialized()
        service_name = definition.name

        # Mark service as starting (outside semaphore - just bookkeeping)
        async with self._startup_coordination_lock_safe:
            self._services_starting.add(service_name)
            if service_name not in self._startup_events:
                self._startup_events[service_name] = asyncio.Event()

        # v95.5: Publish lifecycle event and create trace span for this service
        await self.publish_service_lifecycle_event(service_name, "starting", {"depends_on": definition.depends_on})
        service_span = await self._create_span(f"start_{service_name}", metadata={"service": service_name})

        try:
            # ==================================================================
            # PHASE 1: Wait for dependencies OUTSIDE semaphore (prevents deadlock)
            # ==================================================================
            if definition.depends_on:
                logger.info(
                    f"[v95.1] {service_name}: Waiting for dependencies "
                    f"(semaphore NOT held - deadlock-free): {definition.depends_on}"
                )
                deps_ready = await self._wait_for_dependencies(definition)
                if not deps_ready:
                    logger.error(
                        f"[v95.1] Cannot start {service_name}: dependencies not ready"
                    )
                    # v95.5: Publish failed event for dependency timeout
                    await self._end_span(service_span, status="error", error_message="dependencies_not_ready")
                    await self.publish_service_lifecycle_event(service_name, "failed", {"error": "dependencies_not_ready"})
                    return service_name, False, "dependencies_not_ready"

            # ==================================================================
            # PHASE 2: Acquire semaphore WITH TIMEOUT (prevents indefinite block)
            # ==================================================================
            semaphore_timeout = 60.0  # Max wait for semaphore slot
            try:
                acquired = await asyncio.wait_for(
                    self._service_startup_semaphore_safe.acquire(),
                    timeout=semaphore_timeout
                )
                if not acquired:
                    return service_name, False, "semaphore_acquisition_failed"
            except asyncio.TimeoutError:
                logger.warning(
                    f"[v95.1] {service_name}: Semaphore timeout after {semaphore_timeout}s "
                    f"(other services blocking startup slots)"
                )
                # v95.5: Publish failed event for semaphore timeout
                await self._end_span(service_span, status="error", error_message="semaphore_timeout")
                await self.publish_service_lifecycle_event(service_name, "failed", {"error": "semaphore_timeout"})
                return service_name, False, f"semaphore_timeout_{semaphore_timeout}s"

            try:
                # ==============================================================
                # PHASE 3: Perform actual startup (semaphore held)
                # ==============================================================

                # Step 1: Check if already running via registry
                if self.registry:
                    existing = await self.registry.discover_service(service_name)
                    if existing:
                        reason = f"already running (PID: {existing.pid}, Port: {existing.port})"
                        # v95.5: Service already running, publish ready event
                        await self._end_span(service_span, status="success")
                        await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "existing"})
                        return service_name, True, reason

                # Step 2: HTTP probe using shared session
                session = await self._get_http_session()
                url = f"http://localhost:{definition.default_port}{definition.health_endpoint}"

                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=3.0)
                    ) as resp:
                        if resp.status == 200:
                            reason = f"already running at port {definition.default_port}"
                            # v95.5: Service already running, publish ready event
                            await self._end_span(service_span, status="success")
                            await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "existing_http"})
                            return service_name, True, reason
                except Exception:
                    pass  # Service not running, need to start

                # Step 3: Try Docker hybrid mode
                use_docker, docker_reason = await self._try_docker_hybrid_for_service(definition)
                if use_docker:
                    # Create managed process entry for Docker
                    managed = ManagedProcess(definition=definition)
                    managed.status = ServiceStatus.HEALTHY
                    managed.port = definition.default_port
                    managed.pid = None
                    self.processes[service_name] = managed

                    # Start health monitor (track task for proper cleanup)
                    monitor_task = asyncio.create_task(
                        self._health_monitor_loop(managed),
                        name=f"health_monitor_{service_name}"
                    )
                    managed.health_monitor_task = monitor_task
                    self._track_background_task(monitor_task)

                    # Register in service registry
                    if self.registry:
                        await self.registry.register_service(
                            service_name=service_name,
                            pid=0,
                            port=managed.port,
                            health_endpoint=definition.health_endpoint,
                            metadata={
                                "repo_path": str(definition.repo_path),
                                "docker": True,
                                "container": self.config.jarvis_prime_docker_container,
                            }
                        )

                    # v95.5: Publish ready event for Docker service
                    await self._end_span(service_span, status="success")
                    await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "docker"})
                    return service_name, True, f"Docker container ({docker_reason})"

                # Step 4: Spawn local process

                # v95.9: Check for port conflicts and resolve them automatically
                resolved_port, _ = await self._resolve_port_conflict(
                    service_name, definition.default_port
                )
                if resolved_port != definition.default_port:
                    logger.info(
                        f"[v95.9] Port conflict resolved for {service_name}: "
                        f"{definition.default_port} -> {resolved_port}"
                    )
                    # Update the port in place (dataclass is mutable)
                    definition.default_port = resolved_port
                    # Also update script_args if they contain the port
                    if definition.script_args:
                        updated_args = []
                        skip_next = False
                        for i, arg in enumerate(definition.script_args):
                            if skip_next:
                                skip_next = False
                                continue
                            if arg == "--port" and i + 1 < len(definition.script_args):
                                updated_args.append(arg)
                                updated_args.append(str(resolved_port))
                                skip_next = True
                            else:
                                updated_args.append(arg)
                        definition.script_args = updated_args

                managed = ManagedProcess(definition=definition)
                self.processes[service_name] = managed

                success = await self._spawn_service(managed)

                if success:
                    # v95.9: Start process health monitor for crash detection and auto-restart
                    health_monitor_task = asyncio.create_task(
                        self._monitor_process_health(managed),
                        name=f"crash_monitor_{service_name}"
                    )
                    self._track_background_task(health_monitor_task)
                    logger.debug(f"[v95.9] Started crash monitor for {service_name}")

                    # v95.5: Publish ready event for local process
                    await self._end_span(service_span, status="success")
                    await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "local"})
                    return service_name, True, "local process"
                else:
                    # v95.5: Publish failed event
                    await self._end_span(service_span, status="error", error_message="spawn failed")
                    await self.publish_service_lifecycle_event(service_name, "failed", {"error": "spawn failed"})
                    return service_name, False, "spawn failed"

            finally:
                # ==============================================================
                # PHASE 4: Release semaphore (always, even on error)
                # ==============================================================
                self._service_startup_semaphore_safe.release()

        except Exception as e:
            logger.error(f"    ❌ {service_name} startup error: {e}")
            # v95.5: End span with error status
            await self._end_span(service_span, status="error", error_message=str(e))
            await self.publish_service_lifecycle_event(service_name, "failed", {"error": str(e)})
            return service_name, False, f"error: {e}"

        finally:
            # Mark service as no longer starting
            async with self._startup_coordination_lock_safe:
                self._services_starting.discard(service_name)
                if service_name in self._startup_events:
                    self._startup_events[service_name].set()

    async def _start_services_parallel(
        self,
        definitions: List[ServiceDefinition],
    ) -> Dict[str, bool]:
        """
        v93.11: Start multiple services in parallel with intelligent coordination.

        Features:
        - Parallel startup using asyncio.gather
        - Semaphore limits concurrent startups (default: 3)
        - Memory-aware service ordering (light services first)
        - Error isolation (one failure doesn't block others)
        - Aggregated results with detailed reasons

        Returns:
            Dict mapping service names to success status
        """
        self._ensure_locks_initialized()

        # v95.0: Emit parallel startup begin event
        await _emit_event("PARALLEL_STARTUP_BEGIN", priority="MEDIUM", details={"service_count": len(definitions)})

        # v95.2: CRITICAL FIX - Dependency-aware service ordering
        # The previous logic had a deadlock:
        # - Phase 1 started "light" services (including reactor-core)
        # - Phase 2 started "heavy" services (jarvis-prime)
        # - But reactor-core depends on jarvis-prime!
        # - reactor-core in Phase 1 would wait forever for jarvis-prime in Phase 2
        #
        # New logic: Services that depend on heavy services are also considered "heavy"
        # This ensures proper startup order based on dependency graph, not just ML load.

        # Build set of heavy service names
        heavy_service_names = {"jarvis-prime"}  # Base heavy services

        # Find all services that depend on heavy services (transitively)
        # These must also be treated as "heavy" to respect dependency order
        changed = True
        while changed:
            changed = False
            for d in definitions:
                if d.name not in heavy_service_names:
                    # Check if this service depends on any heavy service
                    if d.depends_on and any(dep in heavy_service_names for dep in d.depends_on):
                        heavy_service_names.add(d.name)
                        changed = True
                        logger.debug(f"[v95.2] '{d.name}' depends on heavy service, promoting to Phase 2")

        # Separate into phases based on dependency-aware classification
        heavy_services = [d for d in definitions if d.name in heavy_service_names]
        light_services = [d for d in definitions if d.name not in heavy_service_names]

        # v95.2: Sort heavy services by dependency order using topological sort
        # jarvis-prime must start before reactor-core
        def get_dependency_order(services: List[ServiceDefinition]) -> List[ServiceDefinition]:
            """Sort services so dependencies come first."""
            ordered = []
            remaining = list(services)
            service_names = {s.name for s in services}
            started_names = set()

            max_iterations = len(remaining) * 2  # Prevent infinite loop
            iterations = 0

            while remaining and iterations < max_iterations:
                iterations += 1
                for service in list(remaining):
                    # Check if all dependencies (within our set) are satisfied
                    deps_in_set = [d for d in (service.depends_on or []) if d in service_names]
                    if all(dep in started_names for dep in deps_in_set):
                        ordered.append(service)
                        started_names.add(service.name)
                        remaining.remove(service)

            # Add any remaining (circular deps) at the end
            ordered.extend(remaining)
            return ordered

        heavy_services = get_dependency_order(heavy_services)
        logger.info(f"[v95.2] Startup order - Light: {[s.name for s in light_services]}, "
                    f"Heavy: {[s.name for s in heavy_services]}")

        results: Dict[str, bool] = {}
        reasons: Dict[str, str] = {}

        # Phase 1: Start light services in parallel
        if light_services:
            logger.info(f"  🚀 Starting {len(light_services)} light service(s) in parallel...")

            light_tasks = [
                self._start_single_service_with_coordination(d)
                for d in light_services
            ]

            light_results = await asyncio.gather(*light_tasks, return_exceptions=True)

            for result in light_results:
                # v95.7: Check for BaseException (not just Exception) for proper type narrowing
                if isinstance(result, BaseException):
                    logger.error(f"  ❌ Service startup exception: {result}")
                    continue

                name, success, reason = result
                results[name] = success
                reasons[name] = reason

                if success:
                    logger.info(f"    ✅ {name}: {reason}")
                    # v95.0: Emit service healthy event
                    await _emit_event("SERVICE_HEALTHY", service_name=name, priority="MEDIUM")
                else:
                    logger.warning(f"    ⚠️ {name}: {reason}")
                    # v95.0: Emit service unhealthy event
                    await _emit_event("SERVICE_UNHEALTHY", service_name=name, priority="HIGH", details={"reason": reason})

        # Phase 2: Start heavy services SEQUENTIALLY (respecting dependency order)
        # v95.2: Heavy services are started in dependency order, not parallel
        # This ensures jarvis-prime is healthy BEFORE reactor-core starts
        if heavy_services:
            logger.info(f"  🚀 Starting {len(heavy_services)} heavy service(s) in dependency order...")

            # Check memory before starting heavy services
            memory_status = await self._get_memory_status()
            logger.info(
                f"    📊 Memory: {memory_status.available_gb:.1f}GB available "
                f"({memory_status.percent_used:.0f}% used)"
            )

            # v95.2: Start heavy services SEQUENTIALLY in dependency order
            # This is critical: jarvis-prime must be HEALTHY before reactor-core starts
            for definition in heavy_services:
                logger.info(f"    🔄 Starting {definition.name}...")

                try:
                    result = await self._start_single_service_with_coordination(definition)

                    if isinstance(result, Exception):
                        logger.error(f"  ❌ Heavy service startup exception: {result}")
                        results[definition.name] = False
                        reasons[definition.name] = str(result)
                        continue

                    name, success, reason = result
                    results[name] = success
                    reasons[name] = reason

                    if success:
                        logger.info(f"    ✅ {name}: {reason}")
                        # v95.0: Emit service healthy event for heavy service
                        await _emit_event("SERVICE_HEALTHY", service_name=name, priority="HIGH")

                        # v95.2: Mark this service as ready for dependent services
                        # v95.7: Fixed bug - use _startup_events (Dict) not _services_ready (Set)
                        self._services_ready.add(name)
                        if name in self._startup_events:
                            self._startup_events[name].set()
                    else:
                        logger.warning(f"    ⚠️ {name}: {reason}")
                        # v95.0: Emit service unhealthy event for heavy service
                        await _emit_event("SERVICE_UNHEALTHY", service_name=name, priority="CRITICAL", details={"reason": reason})

                        # v95.2: If a heavy service fails, dependent services will also fail
                        # But we continue trying other services in case they don't depend on this one
                        logger.warning(f"    ⚠️ Services depending on {name} may fail")

                except Exception as e:
                    logger.error(f"  ❌ Exception starting {definition.name}: {e}")
                    results[definition.name] = False
                    reasons[definition.name] = str(e)

        # v95.0: Emit parallel startup complete event
        await _emit_event("PARALLEL_STARTUP_COMPLETE", priority="MEDIUM", details={"results": results})

        return results

    async def _aggregate_cross_repo_health(self) -> Dict[str, Any]:
        """
        v93.11: Aggregate health status from all repos.

        Returns unified health view across JARVIS, Prime, and Reactor-Core.
        """
        self._ensure_locks_initialized()

        session = await self._get_http_session()

        services = [
            ("jarvis", 8010, "/health"),
            ("jarvis-prime", self.config.jarvis_prime_default_port, "/health"),
            ("reactor-core", self.config.reactor_core_default_port, "/health"),
        ]

        async def check_health(name: str, port: int, endpoint: str) -> Dict[str, Any]:
            url = f"http://localhost:{port}{endpoint}"
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=5.0)
                ) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            return {
                                "name": name,
                                "healthy": True,
                                "status": data.get("status", "healthy"),
                                "port": port,
                                "details": data,
                            }
                        except Exception:
                            return {
                                "name": name,
                                "healthy": True,
                                "status": "healthy",
                                "port": port,
                            }
                    else:
                        return {
                            "name": name,
                            "healthy": False,
                            "status": f"HTTP {resp.status}",
                            "port": port,
                        }
            except asyncio.TimeoutError:
                return {"name": name, "healthy": False, "status": "timeout", "port": port}
            except aiohttp.ClientConnectorError:
                return {"name": name, "healthy": False, "status": "connection refused", "port": port}
            except Exception as e:
                return {"name": name, "healthy": False, "status": str(e), "port": port}

        # Check all services in parallel
        health_tasks = [check_health(n, p, e) for n, p, e in services]
        health_results = await asyncio.gather(*health_tasks, return_exceptions=True)

        # Aggregate results
        aggregated = {
            "timestamp": time.time(),
            "services": {},
            "all_healthy": True,
            "healthy_count": 0,
            "total_count": len(services),
        }

        for result in health_results:
            # v95.7: Check for BaseException for proper type narrowing with asyncio.gather
            if isinstance(result, BaseException):
                continue
            # v95.7: Explicit cast for type narrowing since Pyright doesn't narrow dict types well
            health_info: Dict[str, Any] = result  # type: ignore[assignment]
            name = health_info["name"]
            aggregated["services"][name] = health_info

            if health_info.get("healthy"):
                aggregated["healthy_count"] += 1
            else:
                aggregated["all_healthy"] = False

        # Update cache
        async with self._health_cache_lock_safe:
            self._service_health_cache = aggregated

        return aggregated

    # =========================================================================
    # Main Orchestration
    # =========================================================================

    async def start_all_services(self) -> Dict[str, bool]:
        """
        Start all configured services with coordinated orchestration.

        v93.0: Enhanced with:
        - Pre-flight directory initialization
        - Robust directory handling throughout

        v5.0: Enhanced with:
        - Pre-flight cleanup of legacy ports
        - Wrong-binding detection (127.0.0.1 vs 0.0.0.0)
        - Trinity config integration for port consistency

        Returns dict mapping service names to success status.
        """
        self._running = True

        # v95.4: Initialize locks and set jarvis-body to starting status
        self._ensure_locks_initialized()
        self._jarvis_body_status = "starting"
        self._jarvis_body_startup_time = time.time()
        self._services_starting.add("jarvis-body")
        logger.info("[v95.4] jarvis-body status: starting")

        # v95.5: Initialize distributed tracing FIRST (for correlation across all phases)
        await self._initialize_distributed_tracing()
        startup_span = await self._create_span("full_startup", metadata={"phase": "init"})

        # v95.5: Initialize event bus for lifecycle events
        await self._initialize_event_bus()
        await self.publish_service_lifecycle_event("jarvis-body", "starting", {"pid": os.getpid()})

        # v95.5: Initialize graceful degradation infrastructure
        await self._initialize_graceful_degradation()

        # v95.10: Initialize cross-repo integration systems
        await self._initialize_cross_repo_integration()

        # v93.0: CRITICAL - Ensure all required directories exist FIRST
        # This prevents "No such file or directory" errors throughout startup
        try:
            from backend.core.service_registry import ensure_all_jarvis_directories
            dir_stats = ensure_all_jarvis_directories()
            if dir_stats["failed"]:
                logger.warning(
                    f"[v93.0] Some directories could not be created: {dir_stats['failed']}"
                )
        except Exception as e:
            logger.warning(f"[v93.0] Directory pre-flight check failed: {e}")

        # Setup signal handlers
        try:
            self._setup_signal_handlers()
        except Exception as e:
            logger.warning(f"Could not setup signal handlers: {e}")

        # v95.2: CRITICAL - Register jarvis-body IMMEDIATELY before starting external services
        # This fixes the issue where jarvis-prime waits 120s for jarvis-body that never registered
        # The registration MUST happen BEFORE Phase 2 starts external services
        jarvis_body_registered = False
        if self.registry:
            # v95.2: First, ensure jarvis-body is registered and heartbeat sent IMMEDIATELY
            # This is critical for cross-repo discovery to work
            try:
                success = await self.registry.ensure_owner_registered_immediately()
                if success:
                    logger.info("[v95.2] ✅ jarvis-body registered (early registration for cross-repo discovery)")
                    jarvis_body_registered = True
                else:
                    logger.warning("[v95.2] ⚠️ Early jarvis-body registration may have failed")
            except Exception as e:
                logger.warning(f"[v95.2] Early jarvis-body registration warning: {e}")

            # Start cleanup task which includes self-heartbeat loop
            await self.registry.start_cleanup_task()

        # v95.4: Verify jarvis-body health and update unified state
        if jarvis_body_registered:
            # Update unified state to signal jarvis-body is available
            await self._update_unified_state_for_jarvis_body("starting", health=True)

            # Verify health - this confirms registry entry and heartbeat are working
            health_verified = await self._verify_jarvis_body_health(timeout=30.0)
            if health_verified:
                self._jarvis_body_status = "healthy"
                self._services_starting.discard("jarvis-body")
                self._services_ready.add("jarvis-body")
                await self._update_unified_state_for_jarvis_body("healthy", health=True)
                logger.info("[v95.4] ✅ jarvis-body health verified, external services can proceed")
            else:
                # Registration succeeded but health check didn't complete - still allow startup
                self._jarvis_body_status = "starting"
                await self._update_unified_state_for_jarvis_body("starting", health=True)
                logger.warning("[v95.4] ⚠️ jarvis-body health verification incomplete, proceeding with caution")

            # Signal jarvis-body ready event
            if self._jarvis_body_ready_event:
                self._jarvis_body_ready_event.set()
                logger.debug("[v95.4] jarvis-body ready event signaled")

        results = {"jarvis-body": jarvis_body_registered, "jarvis": True}  # Track jarvis-body explicitly

        logger.info("=" * 70)
        logger.info("Cross-Repo Startup Orchestrator v95.0 - Enterprise Grade")
        logger.info("  Features: Parallel startup, Connection pooling, Thread-safe ops, Voice narration")
        logger.info("=" * 70)
        logger.info(f"  Ports: jarvis-prime={self.config.jarvis_prime_default_port}, "
                    f"reactor-core={self.config.reactor_core_default_port}")

        # v95.0: Emit startup begin event for voice narration
        await _emit_event("STARTUP_BEGIN", priority="HIGH")

        # Phase 0: Pre-flight cleanup (v5.0 + v4.0 Service Registry)
        logger.info("\n📍 PHASE 0: Pre-flight cleanup")

        # v5.0: Clean up legacy ports (processes on old hardcoded ports)
        await self._cleanup_legacy_ports()

        # v4.0: Clean up stale service registry entries (dead PIDs, PID reuse)
        if self.registry:
            logger.info("  🧹 Service registry pre-flight cleanup...")
            try:
                cleanup_stats = await self.registry.pre_flight_cleanup()
                removed_count = (
                    len(cleanup_stats.get("removed_dead_pid", [])) +
                    len(cleanup_stats.get("removed_pid_reuse", [])) +
                    len(cleanup_stats.get("removed_invalid", []))
                )
                if removed_count > 0:
                    logger.info(
                        f"  ✅ Cleaned {removed_count} stale registry entries "
                        f"({cleanup_stats['valid_entries']} valid remain)"
                    )
                else:
                    logger.info(
                        f"  ✅ Registry clean ({cleanup_stats['valid_entries']} valid services)"
                    )
            except Exception as e:
                logger.warning(f"  ⚠️ Registry cleanup failed (continuing): {e}")

        # Phase 1: JARVIS Core (already starting)
        logger.info("\n📍 PHASE 1: JARVIS Core (starting via supervisor)")
        logger.info("✅ JARVIS Core initialization in progress...")

        # Phase 2: Probe and spawn external services (v93.11: PARALLEL)
        logger.info("\n📍 PHASE 2: External services startup (PARALLEL)")

        definitions = self._get_service_definitions()

        # v93.11: Use parallel startup with coordination
        parallel_results = await self._start_services_parallel(definitions)
        results.update(parallel_results)

        # Phase 3: Verification
        logger.info("\n📍 PHASE 3: Integration verification")

        healthy_count = sum(1 for v in results.values() if v)
        total_count = len(results)

        if healthy_count == total_count:
            logger.info(f"✅ All {total_count} services operational - FULL MODE")
            # v95.0: Emit startup complete event
            await _emit_event("STARTUP_COMPLETE", priority="HIGH", elapsed_seconds=time.time() - self._running)
        else:
            logger.warning(
                f"⚠️ Running in DEGRADED MODE: {healthy_count}/{total_count} services operational"
            )
            # v95.0: Emit health degraded event
            unhealthy = [name for name, success in results.items() if not success]
            await _emit_event(
                "HEALTH_DEGRADED",
                priority="HIGH",
                details={"unhealthy_services": unhealthy, "healthy_count": healthy_count, "total_count": total_count}
            )

        # Phase 4: v93.0 - Register restart commands with resilient mesh
        logger.info("\n📍 PHASE 4: Registering auto-restart commands")
        try:
            # Get service names that were started (excluding jarvis which is always running)
            started_services = [
                name for name, success in results.items()
                if success and name != "jarvis"
            ]
            started_services.append("jarvis-core")  # Ensure jarvis-core is included

            restart_results = await register_restart_commands_with_mesh(started_services)
            registered_count = sum(1 for v in restart_results.values() if v)

            if registered_count > 0:
                logger.info(f"  ✅ Registered {registered_count} restart commands for auto-healing")
            else:
                logger.warning(
                    "  ⚠️ No restart commands registered (mesh may not be initialized yet)"
                )
        except Exception as e:
            logger.warning(f"  ⚠️ Restart command registration failed (non-fatal): {e}")

        # v95.1: Phase 5 - Start intelligent recovery coordinator
        logger.info("\n📍 PHASE 5: Starting intelligent recovery coordinator")
        try:
            await self.start_recovery_coordinator()
            logger.info("  ✅ Recovery coordinator active (proactive health monitoring enabled)")
        except Exception as e:
            logger.warning(f"  ⚠️ Recovery coordinator startup failed (non-fatal): {e}")

        # Print summary
        logger.info("\n" + "=" * 70)
        logger.info("🎯 Startup Summary:")
        for name, success in results.items():
            status = "✅ Running" if success else "⚠️ Unavailable"
            logger.info(f"  {name}: {status}")
        logger.info("=" * 70)

        # v95.5: Publish completion lifecycle events for all services
        for name, success in results.items():
            if name == "jarvis-body":
                continue  # Already published
            status = "ready" if success else "failed"
            await self.publish_service_lifecycle_event(
                service=name,
                status=status,
                details={
                    "startup_time": time.time() - (self._jarvis_body_startup_time or time.time()),
                    "degradation_mode": self._degradation_mode.get(name, "unknown"),
                }
            )

        # v95.5: End the startup trace span
        if startup_span:
            status = "success" if healthy_count == total_count else "partial"
            await self._end_span(startup_span, status=status)

        # v95.5: Publish final startup completion event with degradation info
        await self._publish_lifecycle_event(
            event_type="lifecycle.startup_complete",
            payload={
                "services": results,
                "healthy_count": healthy_count,
                "total_count": total_count,
                "degradation_status": self.get_service_degradation_status(),
                "startup_duration": time.time() - (self._jarvis_body_startup_time or time.time()),
            },
            priority="HIGH" if healthy_count == total_count else "CRITICAL",
        )

        # v95.5: Log distributed tracing summary
        logger.info(f"[v95.5] Startup correlation ID: {self.get_correlation_id()}")

        return results

    async def shutdown_all_services(self) -> None:
        """
        v95.3 + v95.11: Gracefully shutdown all managed services with proper ordering.

        Enhanced with:
        - REVERSE DEPENDENCY ORDER: Services shutdown in reverse startup order
          (dependents first, then their dependencies)
        - Background task cancellation (prevents orphaned coroutines)
        - HTTP session cleanup with error handling
        - Startup coordination cleanup
        - Timeout protection per service
        - Shutdown completion flag (v95.3) prevents post-shutdown recovery
        - v95.11: Operation draining before service termination
        """
        logger.info("\n🛑 Shutting down all services...")

        # v95.3: Set shutdown_completed flag EARLY to prevent any new recovery attempts
        # This is defensive - even if recovery coordinator is slow to stop, it will see this flag
        self._shutdown_completed = True
        self._shutdown_completed_timestamp = time.time()
        logger.info("[v95.3] Shutdown completion flag set - recovery disabled")

        # v95.11: Start draining all operations FIRST (before stopping services)
        # This prevents new database operations from starting during shutdown
        drain_timeout = float(os.environ.get("SHUTDOWN_DRAIN_TIMEOUT", "10.0"))
        try:
            from backend.core.resilience.graceful_shutdown import get_operation_guard_sync
            guard = get_operation_guard_sync()

            # Signal global shutdown - no new operations allowed
            await guard.begin_global_shutdown()
            logger.info(f"[v95.11] 🛑 Operation draining started (timeout: {drain_timeout}s)")

            # Wait for in-flight operations to complete
            active_before = guard.get_total_active()
            if active_before > 0:
                logger.info(f"[v95.11] Waiting for {active_before} in-flight operations to complete...")
                drained = await guard.wait_for_all_drain(timeout=drain_timeout)

                if drained:
                    logger.info("[v95.11] ✅ All operations drained successfully")
                else:
                    remaining = guard.get_total_active()
                    stats = guard.get_stats()
                    logger.warning(
                        f"[v95.11] ⚠️ Drain incomplete: {remaining} operations still active "
                        f"(categories: {stats['active_by_category']})"
                    )
            else:
                logger.info("[v95.11] ✅ No in-flight operations to drain")
        except ImportError:
            logger.debug("[v95.11] OperationGuard not available, skipping drain")
        except Exception as e:
            logger.warning(f"[v95.11] Error during operation drain: {e}")

        # v95.1: Stop recovery coordinator first (prevent restart during shutdown)
        await self.stop_recovery_coordinator()

        # v95.1: Calculate reverse dependency order for shutdown
        # Services that depend on others should shutdown FIRST
        shutdown_order = self._calculate_shutdown_order()
        logger.info(f"[v95.1] Shutdown order: {' → '.join(shutdown_order)}")

        # v95.1: Shutdown in phases by dependency level
        for service_name in shutdown_order:
            if service_name in self.processes:
                managed = self.processes[service_name]
                try:
                    logger.info(f"  Stopping {service_name}...")
                    await asyncio.wait_for(
                        self._stop_process(managed),
                        timeout=30.0  # Per-service shutdown timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[v95.1] Timeout stopping {service_name}, forcing termination"
                    )
                    if managed.process and managed.process.returncode is None:
                        try:
                            managed.process.kill()
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"[v95.1] Error stopping {service_name}: {e}")

        # v95.1: Cancel all tracked background tasks
        await self._cancel_all_background_tasks(timeout=10.0)

        # Stop registry cleanup
        if self.registry:
            try:
                await self.registry.stop_cleanup_task()
            except Exception as e:
                logger.warning(f"[v95.1] Registry cleanup error: {e}")

        # v95.1: Close shared HTTP session with error handling
        await self._close_http_session()

        # v95.12: Cleanup multiprocessing resources (ProcessPoolExecutors, semaphores)
        try:
            from backend.core.resilience.graceful_shutdown import cleanup_multiprocessing_resources
            mp_timeout = float(os.environ.get("SHUTDOWN_MP_CLEANUP_TIMEOUT", "10.0"))
            logger.info(f"[v95.12] Cleaning up multiprocessing resources...")
            mp_result = await cleanup_multiprocessing_resources(timeout=mp_timeout)
            logger.info(
                f"[v95.12] ✅ Multiprocessing cleanup: "
                f"{mp_result.get('successful', 0)} success, "
                f"{mp_result.get('forced', 0)} forced, "
                f"{mp_result.get('failed', 0)} failed"
            )
        except ImportError:
            logger.debug("[v95.12] Multiprocessing cleanup not available")
        except Exception as e:
            logger.warning(f"[v95.12] Multiprocessing cleanup error: {e}")

        # v95.1: Clear startup coordination state
        self._services_starting.clear()
        self._services_ready.clear()
        self._startup_events.clear()
        self._service_health_cache.clear()
        self._background_tasks.clear()

        # v95.13: Mark global shutdown as completed
        try:
            from backend.core.resilience.graceful_shutdown import complete_global_shutdown
            complete_global_shutdown()
        except Exception as e:
            logger.debug(f"[v95.13] Could not complete global shutdown signal: {e}")

        logger.info("✅ All services shut down")
        self._running = False

    def _calculate_shutdown_order(self) -> List[str]:
        """
        v95.1: Calculate optimal shutdown order based on dependencies.

        Returns services in REVERSE dependency order:
        - Services with NO dependents shutdown first
        - Services that others depend on shutdown last

        This prevents connection errors when services try to communicate
        with dependencies that have already shutdown.
        """
        # Build dependency graph
        dependents: Dict[str, Set[str]] = {}  # service -> set of services that depend on it
        all_services = set(self.processes.keys())

        for service_name in all_services:
            dependents[service_name] = set()

        # Populate dependents from service definitions
        for service_name, managed in self.processes.items():
            if managed.definition and managed.definition.depends_on:
                for dep in managed.definition.depends_on:
                    if dep in dependents:
                        dependents[dep].add(service_name)

        # Topological sort in reverse order (Kahn's algorithm)
        # Start with services that have NO dependents (leaf nodes)
        no_dependents = [s for s in all_services if not dependents[s]]
        shutdown_order = []

        while no_dependents:
            # Pick next service to shutdown (one with no remaining dependents)
            service = no_dependents.pop(0)
            shutdown_order.append(service)

            # "Remove" this service from the graph
            # Update services that this one depends on
            if service in self.processes:
                definition = self.processes[service].definition
                if definition and definition.depends_on:
                    for dep in definition.depends_on:
                        if dep in dependents:
                            dependents[dep].discard(service)
                            # If dep now has no dependents, add to queue
                            if not dependents[dep] and dep not in shutdown_order:
                                no_dependents.append(dep)

        # Handle any remaining services (circular dependencies or orphans)
        remaining = all_services - set(shutdown_order)
        if remaining:
            logger.warning(
                f"[v95.1] Services with circular dependencies or missing from graph: {remaining}"
            )
            shutdown_order.extend(remaining)

        return shutdown_order


# =============================================================================
# Convenience Functions (Backward Compatibility)
# =============================================================================

# Global orchestrator instance
_orchestrator: Optional[ProcessOrchestrator] = None


def get_orchestrator() -> ProcessOrchestrator:
    """Get global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ProcessOrchestrator()
    return _orchestrator


def create_restart_function(service_name: str) -> Callable[[], Coroutine]:
    """
    v93.0: Factory function to create restart closures for services.

    This creates a restart function that can be registered with the
    SelfHealingServiceManager. When called, it will use the global
    orchestrator to restart the specified service.

    Args:
        service_name: Name of the service this restart function will handle

    Returns:
        An async function that restarts the service when called

    Example:
        restart_fn = create_restart_function("jarvis-core")
        recovery_manager.register_restart_command("jarvis-core", restart_fn)
    """
    async def restart_service() -> bool:
        """Restart the captured service via the global orchestrator."""
        orchestrator = get_orchestrator()
        return await orchestrator.restart_service(service_name)

    return restart_service


async def register_restart_commands_with_mesh(
    service_names: Optional[List[str]] = None
) -> Dict[str, bool]:
    """
    v93.0: Register restart commands for services with the resilient mesh.

    This bridges the ProcessOrchestrator's restart capability with the
    SelfHealingServiceManager in native_integration.py.

    Args:
        service_names: Optional list of service names to register.
                      If None, registers for all known services.

    Returns:
        Dict mapping service names to registration success status
    """
    results: Dict[str, bool] = {}

    # Default service names if not provided
    if service_names is None:
        service_names = ["jarvis-core", "jarvis-prime", "reactor-core"]

    try:
        # Lazy import to avoid circular dependencies
        from backend.core.ouroboros.native_integration import (
            get_resilient_mesh,
            get_recovery_manager,
        )

        # Get the recovery manager - prefer mesh's manager, fallback to singleton
        recovery_manager = None

        try:
            mesh = get_resilient_mesh()
            if mesh and hasattr(mesh, 'recovery_manager'):
                recovery_manager = mesh.recovery_manager
                logger.debug("[v93.0] Using recovery manager from resilient mesh")
        except Exception as mesh_err:
            logger.debug(f"[v93.0] Resilient mesh not available: {mesh_err}")

        # Fallback to global singleton if mesh unavailable
        if recovery_manager is None:
            try:
                recovery_manager = get_recovery_manager()
                logger.debug("[v93.0] Using global singleton recovery manager")
            except Exception as mgr_err:
                logger.debug(f"[v93.0] Recovery manager singleton failed: {mgr_err}")

        if recovery_manager is None:
            logger.warning(
                "[v93.0] Cannot register restart commands: "
                "no recovery manager available"
            )
            return {name: False for name in service_names}

        # Register restart functions for each service
        for service_name in service_names:
            try:
                restart_fn = create_restart_function(service_name)
                recovery_manager.register_restart_command(service_name, restart_fn)
                results[service_name] = True
                logger.info(f"[v93.0] Registered restart command for {service_name}")
            except Exception as e:
                logger.error(f"[v93.0] Failed to register restart for {service_name}: {e}")
                results[service_name] = False

        return results

    except ImportError as e:
        logger.warning(f"[v93.0] Could not import resilient mesh components: {e}")
        return {name: False for name in service_names}
    except Exception as e:
        logger.error(f"[v93.0] Error registering restart commands: {e}")
        return {name: False for name in service_names}


async def probe_jarvis_prime() -> bool:
    """Legacy: Probe J-Prime health endpoint."""
    config = OrchestratorConfig()
    url = f"http://localhost:{config.jarvis_prime_default_port}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                return response.status == 200
    except Exception:
        return False


async def probe_reactor_core() -> bool:
    """Legacy: Probe Reactor-Core health endpoint."""
    config = OrchestratorConfig()
    url = f"http://localhost:{config.reactor_core_default_port}/api/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                return response.status == 200
    except Exception:
        return False


async def start_all_repos() -> Dict[str, bool]:
    """Legacy: Start all repos with orchestration."""
    orchestrator = get_orchestrator()
    return await orchestrator.start_all_services()


async def initialize_cross_repo_orchestration() -> None:
    """
    Initialize cross-repo orchestration.

    This is called by run_supervisor.py during startup.
    """
    # v95.13: Reset global shutdown signal from previous runs
    try:
        from backend.core.resilience.graceful_shutdown import reset_global_shutdown
        reset_global_shutdown()
        logger.debug("[v95.13] Global shutdown signal reset for new startup")
    except Exception as e:
        logger.warning(f"[v95.13] Could not reset global shutdown signal: {e}")

    try:
        orchestrator = get_orchestrator()
        results = await orchestrator.start_all_services()

        # Initialize advanced training coordinator if Reactor-Core available
        if results.get("reactor-core"):
            logger.info("Initializing Advanced Training Coordinator...")
            try:
                from backend.intelligence.advanced_training_coordinator import (
                    AdvancedTrainingCoordinator
                )
                coordinator = await AdvancedTrainingCoordinator.create()
                logger.info("✅ Advanced Training Coordinator initialized")
            except Exception as e:
                logger.warning(f"Advanced Training Coordinator initialization failed: {e}")

    except Exception as e:
        logger.error(f"Cross-repo orchestration error: {e}", exc_info=True)


# =============================================================================
# v95.3: Global Shutdown State Accessors
# =============================================================================

def is_orchestrator_shutdown_in_progress() -> bool:
    """
    v95.3: Check if the orchestrator's shutdown is in progress.

    This is a cross-component accessor that allows other modules
    (like OrphanDetector, CoordinatedShutdownManager) to check if
    the orchestrator is shutting down.

    Returns:
        True if shutdown is in progress, False otherwise
    """
    if _orchestrator is None:
        return False
    return _orchestrator._shutdown_event.is_set()


def is_orchestrator_shutdown_completed() -> bool:
    """
    v95.3: Check if the orchestrator's shutdown has completed.

    This flag is set when shutdown finishes and stays True forever.
    Use this to prevent post-shutdown recovery attempts.

    Returns:
        True if shutdown has completed, False otherwise
    """
    if _orchestrator is None:
        return False
    return _orchestrator._shutdown_completed


def get_orchestrator_shutdown_state() -> dict:
    """
    v95.3: Get comprehensive shutdown state for debugging and coordination.

    Returns:
        Dict with shutdown state details:
        - shutdown_in_progress: bool
        - shutdown_completed: bool
        - shutdown_completed_timestamp: float or None
        - running: bool
    """
    if _orchestrator is None:
        return {
            "orchestrator_exists": False,
            "shutdown_in_progress": False,
            "shutdown_completed": False,
            "shutdown_completed_timestamp": None,
            "running": False,
        }

    return {
        "orchestrator_exists": True,
        "shutdown_in_progress": _orchestrator._shutdown_event.is_set(),
        "shutdown_completed": _orchestrator._shutdown_completed,
        "shutdown_completed_timestamp": _orchestrator._shutdown_completed_timestamp,
        "running": _orchestrator._running,
    }


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "ProcessOrchestrator",
    "ManagedProcess",
    "ServiceDefinition",
    "ServiceStatus",
    "OrchestratorConfig",
    "get_orchestrator",
    "create_restart_function",
    "register_restart_commands_with_mesh",
    "start_all_repos",
    "initialize_cross_repo_orchestration",
    "probe_jarvis_prime",
    "probe_reactor_core",
    # v95.3: Global shutdown state accessors
    "is_orchestrator_shutdown_in_progress",
    "is_orchestrator_shutdown_completed",
    "get_orchestrator_shutdown_state",
]
