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
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

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
    v93.9: Circuit Breaker pattern implementation for Docker operations.

    Prevents cascading failures by:
    - Tracking consecutive failures
    - Opening circuit after failure threshold
    - Allowing recovery testing after timeout
    - Closing circuit on successful recovery
    """
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    half_open_max_requests: int = 1

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_requests: int = 0
    success_count: int = 0

    def record_success(self) -> None:
        """Record a successful operation."""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_max_requests:
                # Recovery successful, close circuit
                self.state = CircuitBreakerState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                logger.info(f"    ✅ Circuit breaker [{self.name}]: CLOSED (recovered)")
        else:
            self.failure_count = 0
            self.success_count += 1

    def record_failure(self) -> None:
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.success_count = 0

        if self.state == CircuitBreakerState.HALF_OPEN:
            # Failed during recovery test, reopen circuit
            self.state = CircuitBreakerState.OPEN
            logger.warning(f"    ⚠️ Circuit breaker [{self.name}]: OPEN (recovery failed)")
        elif self.failure_count >= self.failure_threshold:
            # Threshold exceeded, open circuit
            self.state = CircuitBreakerState.OPEN
            logger.warning(
                f"    ⚠️ Circuit breaker [{self.name}]: OPEN "
                f"({self.failure_count} failures)"
            )

    def can_execute(self) -> bool:
        """Check if operation can proceed."""
        if self.state == CircuitBreakerState.CLOSED:
            return True

        if self.state == CircuitBreakerState.OPEN:
            # Check if recovery timeout has passed
            if (time.time() - self.last_failure_time) >= self.recovery_timeout:
                self.state = CircuitBreakerState.HALF_OPEN
                self.half_open_requests = 0
                logger.info(f"    🔄 Circuit breaker [{self.name}]: HALF_OPEN (testing)")
                return True
            return False

        if self.state == CircuitBreakerState.HALF_OPEN:
            # Allow limited requests during half-open
            if self.half_open_requests < self.half_open_max_requests:
                self.half_open_requests += 1
                return True
            return False

        return False


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
    v95.0: Added dynamic script discovery and validation methods.
    """
    name: str
    repo_path: Path
    script_name: str = "main.py"
    fallback_scripts: List[str] = field(default_factory=lambda: ["server.py", "run.py", "app.py"])
    default_port: int = 8000
    health_endpoint: str = "/health"
    startup_timeout: float = 60.0
    environment: Dict[str, str] = field(default_factory=dict)

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
    _CANONICAL_DEFINITIONS = {
        "jarvis-prime": {
            "script_name": "run_server.py",
            "fallback_scripts": ["main.py", "server.py", "app.py"],
            "default_port_env": "JARVIS_PRIME_PORT",
            "default_port": 8000,
            "health_endpoint": "/health",
            "startup_timeout": 180.0,  # ML model loading
            "repo_path_env": "JARVIS_PRIME_PATH",
            "default_repo_path": "~/Documents/repos/jarvis-prime",
            "script_args_factory": lambda port: ["--port", str(port), "--host", "0.0.0.0"],
            "environment_factory": lambda path: {
                "PYTHONPATH": str(path),
                "PYTHONWARNINGS": "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning",
                "TF_CPP_MIN_LOG_LEVEL": "2",
                "TRANSFORMERS_VERBOSITY": "error",
                "TOKENIZERS_PARALLELISM": "false",
                "COREMLTOOLS_LOG_LEVEL": "ERROR",
            },
        },
        "reactor-core": {
            "script_name": "run_reactor.py",
            "fallback_scripts": ["run_supervisor.py", "main.py", "server.py"],
            "default_port_env": "REACTOR_CORE_PORT",
            "default_port": 8090,
            "health_endpoint": "/health",
            "startup_timeout": 90.0,
            "repo_path_env": "REACTOR_CORE_PATH",
            "default_repo_path": "~/Documents/repos/reactor-core",
            "script_args_factory": lambda port: ["--port", str(port)],
            "environment_factory": lambda path: {
                "PYTHONPATH": str(path),
                "REACTOR_PORT": str(os.getenv("REACTOR_CORE_PORT", 8090)),
            },
            "use_uvicorn": False,
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

        # Resolve path
        repo_path = path_override
        if repo_path is None:
            env_path = os.getenv(canonical["repo_path_env"])
            if env_path:
                repo_path = Path(env_path).expanduser()
            else:
                repo_path = Path(canonical["default_repo_path"]).expanduser()

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
    def list_services(cls) -> List[str]:
        """List all known service names."""
        return list(cls._CANONICAL_DEFINITIONS.keys())

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the definition cache."""
        cls._cache.clear()


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
        if self._health_cache_lock is None:
            self._health_cache_lock = asyncio.Lock()

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """
        v93.11: Get shared HTTP session with connection pooling.

        Creates session lazily and reuses it for all HTTP requests.
        Connection pooling improves performance and reduces resource usage.
        """
        self._ensure_locks_initialized()

        async with self._http_session_lock:
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

                self._http_session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                    raise_for_status=False,
                )

                logger.debug("[v93.11] Shared HTTP session initialized with connection pooling")

            return self._http_session

    async def _close_http_session(self) -> None:
        """
        v93.11: Close shared HTTP session gracefully.

        Called during shutdown.
        """
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            # Allow connections to close gracefully
            await asyncio.sleep(0.25)
            self._http_session = None
            logger.debug("[v93.11] Shared HTTP session closed")

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

        async with self._circuit_breaker_lock:
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

                if "starting" in status or "model loading" in status:
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
    _SUPPRESSED_WARNING_PATTERNS = [
        'scikit-learn version',
        'is not supported',
        'minimum required version',
        'maximum required version',
        'disabling scikit-learn conversion api',
        'torch version',
        'has not been tested',
        'coremltools',
        'unexpected errors',
        'most recent version that has been tested',
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
            while not self._shutdown_event.is_set():
                await asyncio.sleep(self.config.health_check_interval)

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
                    logger.warning(
                        f"🚨 Process {managed.definition.name} died (exit code: {exit_code})"
                    )
                    managed.status = ServiceStatus.FAILED

                    if self.config.auto_healing_enabled:
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

                    # Log status transition only once
                    if managed.status != ServiceStatus.HEALTHY:
                        managed.status = ServiceStatus.HEALTHY
                        logger.info(f"✅ {managed.definition.name} is healthy")

                    # ═══════════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Send heartbeat on EVERY successful health check
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
    # Auto-Healing
    # =========================================================================

    async def _auto_heal(self, managed: ManagedProcess) -> bool:
        """
        v95.0: Attempt to restart a failed service with exponential backoff and event emissions.

        Returns True if restart succeeded.
        """
        if managed.restart_count >= self.config.max_restart_attempts:
            logger.error(
                f"❌ {managed.definition.name} exceeded max restart attempts "
                f"({self.config.max_restart_attempts}). Giving up."
            )
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service crashed event (gave up)
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=managed.definition.name,
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
            f"🔄 Restarting {managed.definition.name} in {backoff:.1f}s "
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

    async def _pre_spawn_validation(self, definition: ServiceDefinition) -> tuple[bool, Optional[str]]:
        """
        Validate a service before spawning.

        v4.0: Pre-launch checks:
        - Repo path exists
        - Script exists
        - Venv Python found (optional but preferred)
        - Port not already in use

        v95.0: Enhanced with detailed diagnostic logging for troubleshooting.

        Returns:
            Tuple of (is_valid, python_executable)
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

        # Check port availability
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', definition.default_port))
            sock.close()
            if result == 0:
                logger.warning(
                    f"    ⚠️ Port {definition.default_port} already in use - "
                    f"{definition.name} may already be running"
                )
                # Not a fatal error - the service might be running
        except Exception as e:
            logger.debug(f"Port check failed: {e}")

        return True, python_exec

    async def _spawn_service(self, managed: ManagedProcess) -> bool:
        """
        v95.0: Spawn a service process with comprehensive event emissions.

        Enhanced with:
        - Pre-spawn validation (venv detection, port check)
        - Better error reporting
        - Environment isolation
        - Real-time voice narration of service lifecycle

        Returns True if spawn and health check succeeded.
        """
        definition = managed.definition

        # v95.0: Emit service spawning event
        await _emit_event(
            "SERVICE_SPAWNING",
            service_name=definition.name,
            priority="HIGH",
            details={
                "port": definition.default_port,
                "repo_path": str(definition.repo_path),
                "startup_timeout": definition.startup_timeout
            }
        )

        # Pre-spawn validation
        is_valid, python_exec = await self._pre_spawn_validation(definition)
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
                cmd = [
                    python_exec, "-m", "uvicorn",
                    definition.uvicorn_app,
                    "--host", "0.0.0.0",
                    "--port", str(definition.default_port),
                ]
                logger.info(f"🚀 Spawning {definition.name} via uvicorn: {definition.uvicorn_app}")

            elif definition.module_path:
                # Module-based entry point (python -m)
                cmd = [python_exec, "-m", definition.module_path]
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

                return True
            else:
                logger.warning(
                    f"⚠️ {definition.name} spawned but did not become healthy "
                    f"within {definition.startup_timeout}s"
                )
                managed.status = ServiceStatus.DEGRADED
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
                return False

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
        # Cancel background tasks
        for task in [managed.output_stream_task, managed.health_monitor_task]:
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
        v93.11: Start a single service with proper coordination and error handling.

        Uses semaphore to limit concurrent startups and prevent resource exhaustion.
        Returns tuple of (service_name, success, reason).
        """
        self._ensure_locks_initialized()

        async with self._service_startup_semaphore:
            service_name = definition.name

            # Mark service as starting
            async with self._startup_coordination_lock:
                self._services_starting.add(service_name)
                if service_name not in self._startup_events:
                    self._startup_events[service_name] = asyncio.Event()

            try:
                # Step 1: Check if already running via registry
                if self.registry:
                    existing = await self.registry.discover_service(service_name)
                    if existing:
                        reason = f"already running (PID: {existing.pid}, Port: {existing.port})"
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

                    # Start health monitor
                    managed.health_monitor_task = asyncio.create_task(
                        self._health_monitor_loop(managed)
                    )

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

                    return service_name, True, f"Docker container ({docker_reason})"

                # Step 4: Spawn local process
                managed = ManagedProcess(definition=definition)
                self.processes[service_name] = managed

                success = await self._spawn_service(managed)

                if success:
                    return service_name, True, "local process"
                else:
                    return service_name, False, "spawn failed"

            except Exception as e:
                logger.error(f"    ❌ {service_name} startup error: {e}")
                return service_name, False, f"error: {e}"

            finally:
                # Mark service as no longer starting
                async with self._startup_coordination_lock:
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

        # Separate heavy (ML-based) and light services
        heavy_services = [d for d in definitions if d.name == "jarvis-prime"]
        light_services = [d for d in definitions if d.name != "jarvis-prime"]

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
                if isinstance(result, Exception):
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

        # Phase 2: Start heavy services (with memory check)
        if heavy_services:
            logger.info(f"  🚀 Starting {len(heavy_services)} heavy service(s)...")

            # Check memory before starting heavy services
            memory_status = await self._get_memory_status()
            logger.info(
                f"    📊 Memory: {memory_status.available_gb:.1f}GB available "
                f"({memory_status.percent_used:.0f}% used)"
            )

            heavy_tasks = [
                self._start_single_service_with_coordination(d)
                for d in heavy_services
            ]

            heavy_results = await asyncio.gather(*heavy_tasks, return_exceptions=True)

            for result in heavy_results:
                if isinstance(result, Exception):
                    logger.error(f"  ❌ Heavy service startup exception: {result}")
                    continue

                name, success, reason = result
                results[name] = success
                reasons[name] = reason

                if success:
                    logger.info(f"    ✅ {name}: {reason}")
                    # v95.0: Emit service healthy event for heavy service
                    await _emit_event("SERVICE_HEALTHY", service_name=name, priority="HIGH")
                else:
                    logger.warning(f"    ⚠️ {name}: {reason}")
                    # v95.0: Emit service unhealthy event for heavy service
                    await _emit_event("SERVICE_UNHEALTHY", service_name=name, priority="CRITICAL", details={"reason": reason})

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
            if isinstance(result, Exception):
                continue
            name = result["name"]
            aggregated["services"][name] = result

            if result.get("healthy"):
                aggregated["healthy_count"] += 1
            else:
                aggregated["all_healthy"] = False

        # Update cache
        async with self._health_cache_lock:
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

        # Start service registry cleanup
        if self.registry:
            await self.registry.start_cleanup_task()

        results = {"jarvis": True}  # JARVIS is already running

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

        # Print summary
        logger.info("\n" + "=" * 70)
        logger.info("🎯 Startup Summary:")
        for name, success in results.items():
            status = "✅ Running" if success else "⚠️ Unavailable"
            logger.info(f"  {name}: {status}")
        logger.info("=" * 70)

        return results

    async def shutdown_all_services(self) -> None:
        """
        v93.11: Gracefully shutdown all managed services.

        Enhanced with:
        - Parallel process shutdown
        - HTTP session cleanup
        - Startup coordination cleanup
        """
        logger.info("\n🛑 Shutting down all services...")

        # Stop all processes in parallel
        shutdown_tasks = [
            self._stop_process(managed)
            for managed in self.processes.values()
        ]

        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        # Stop registry cleanup
        if self.registry:
            await self.registry.stop_cleanup_task()

        # v93.11: Close shared HTTP session
        await self._close_http_session()

        # v93.11: Clear startup coordination state
        self._services_starting.clear()
        self._services_ready.clear()
        self._startup_events.clear()
        self._service_health_cache.clear()

        logger.info("✅ All services shut down")
        self._running = False


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
]
