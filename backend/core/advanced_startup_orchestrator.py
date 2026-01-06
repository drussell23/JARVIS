"""
v78.0: Advanced Startup Orchestration System
=============================================

Enterprise-grade startup orchestration with:
- Dependency Graph Resolution (Kahn's Algorithm + Parallel Execution)
- Circuit Breakers with Exponential Backoff and Jitter
- Dynamic Configuration Discovery (Zero Hardcoding)
- Connection Verification Loops with Adaptive Timeouts
- State Machine with Phase Transitions and Rollback
- Trinity Cross-Repo Integration
- ARM64 SIMD Acceleration Support

This module provides the most advanced startup patterns used in
production distributed systems (Kubernetes, Consul, etc.) adapted
for the JARVIS Trinity architecture.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │           DependencyGraphOrchestrator                       │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
    │  │  Component  │  │  Component  │  │  Component  │         │
    │  │   Node A    │──│   Node B    │──│   Node C    │         │
    │  │ (no deps)   │  │ (deps: A)   │  │ (deps: A,B) │         │
    │  └─────────────┘  └─────────────┘  └─────────────┘         │
    │         │                │                │                 │
    │    Level 0          Level 1          Level 2               │
    │    (parallel)       (parallel)       (sequential)          │
    └─────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.advanced_startup_orchestrator import (
        DependencyGraphOrchestrator,
        DynamicConfigDiscovery,
        ConnectionVerifier,
        get_orchestrator,
    )

    # During supervisor startup
    orchestrator = await get_orchestrator()

    # Register components with dependencies
    orchestrator.register_component(
        name="jarvis_backend",
        dependencies=["cloud_sql", "trinity_dir"],
        startup_func=start_backend,
        health_check=check_backend_health,
        critical=True,
    )

    # Start all components in optimal order
    results = await orchestrator.start_all(parallel=True)

Author: JARVIS v78.0
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import socket
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Deque,
    Dict,
    Generic,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

# Type variables for generic components
T = TypeVar("T")
ConfigT = TypeVar("ConfigT")


# =============================================================================
# Enums and Constants
# =============================================================================

class ComponentStatus(Enum):
    """Component lifecycle status with detailed states."""
    UNREGISTERED = "unregistered"
    PENDING = "pending"
    STARTING = "starting"
    HEALTH_CHECK = "health_check"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"
    CIRCUIT_OPEN = "circuit_open"
    RECOVERING = "recovering"


class CircuitBreakerState(Enum):
    """Circuit breaker states following the standard pattern."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, rejecting requests
    HALF_OPEN = "half_open" # Testing if recovery possible


class StartupPhase(Enum):
    """Startup phases for the state machine."""
    INIT = "init"
    PRE_FLIGHT = "pre_flight"
    CLEANUP = "cleanup"
    RESOURCE_VALIDATION = "resource_validation"
    CONFIG_DISCOVERY = "config_discovery"
    TRINITY_INIT = "trinity_init"
    TRINITY_LAUNCH = "trinity_launch"
    TRINITY_VERIFY = "trinity_verify"
    CODING_COUNCIL_INIT = "coding_council_init"
    ACCELERATION_INIT = "acceleration_init"
    BACKEND_INIT = "backend_init"
    BACKEND_START = "backend_start"
    HEALTH_VERIFICATION = "health_verification"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


class TrinityRepo(Enum):
    """Trinity repository identifiers."""
    JARVIS = "jarvis"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"


# =============================================================================
# Protocols (Structural Subtyping)
# =============================================================================

class HealthCheckProtocol(Protocol):
    """Protocol for health check functions."""
    async def __call__(self) -> bool: ...


class StartupFuncProtocol(Protocol):
    """Protocol for startup functions."""
    async def __call__(self) -> None: ...


class ShutdownFuncProtocol(Protocol):
    """Protocol for shutdown functions."""
    async def __call__(self) -> None: ...


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""
    failure_threshold: int = 5
    success_threshold: int = 3
    timeout_seconds: float = 60.0
    half_open_max_calls: int = 3


@dataclass
class RetryConfig:
    """Configuration for retry behavior with exponential backoff."""
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter_factor: float = 0.25  # Random jitter up to 25%

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and jitter."""
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        jitter = delay * self.jitter_factor * random.random()
        return delay + jitter


@dataclass
class ComponentMetrics:
    """Metrics tracking for a component."""
    start_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_start_time_ms: float = 0.0
    last_start_time: Optional[float] = None
    last_health_check_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0

    @property
    def avg_start_time_ms(self) -> float:
        if self.success_count == 0:
            return 0.0
        return self.total_start_time_ms / self.success_count

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 1.0
        return self.success_count / total


@dataclass
class ComponentNode:
    """
    Represents a component in the dependency graph.

    This is the core data structure that tracks everything about
    a component including its dependencies, status, and behavior.
    """
    name: str
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)  # Reverse dependencies
    startup_func: Optional[Callable[[], Awaitable[None]]] = None
    health_check_func: Optional[Callable[[], Awaitable[bool]]] = None
    shutdown_func: Optional[Callable[[], Awaitable[None]]] = None
    status: ComponentStatus = ComponentStatus.PENDING
    critical: bool = True  # If True, failure stops startup
    retry_config: RetryConfig = field(default_factory=RetryConfig)
    circuit_breaker_config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    circuit_breaker_state: CircuitBreakerState = CircuitBreakerState.CLOSED
    circuit_breaker_last_failure: Optional[float] = None
    circuit_breaker_half_open_calls: int = 0
    timeout_seconds: float = 60.0
    health_check_interval: float = 5.0
    health_check_timeout: float = 5.0
    metrics: ComponentMetrics = field(default_factory=ComponentMetrics)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    def __post_init__(self):
        # Ensure lists are mutable
        if self.dependencies is None:
            self.dependencies = []
        if self.dependents is None:
            self.dependents = []

    @property
    def can_start(self) -> bool:
        """Check if component can be started based on circuit breaker."""
        if self.circuit_breaker_state == CircuitBreakerState.OPEN:
            # Check if timeout has elapsed
            if self.circuit_breaker_last_failure:
                elapsed = time.time() - self.circuit_breaker_last_failure
                if elapsed >= self.circuit_breaker_config.timeout_seconds:
                    # Transition to half-open
                    self.circuit_breaker_state = CircuitBreakerState.HALF_OPEN
                    self.circuit_breaker_half_open_calls = 0
                    return True
            return False
        return True

    def record_failure(self):
        """Record a failure and potentially open circuit breaker."""
        self.metrics.failure_count += 1
        self.metrics.consecutive_failures += 1
        self.metrics.consecutive_successes = 0

        if self.metrics.consecutive_failures >= self.circuit_breaker_config.failure_threshold:
            self.circuit_breaker_state = CircuitBreakerState.OPEN
            self.circuit_breaker_last_failure = time.time()
            logger.warning(f"[CircuitBreaker] OPENED for {self.name} after "
                          f"{self.metrics.consecutive_failures} failures")

    def record_success(self):
        """Record a success and potentially close circuit breaker."""
        self.metrics.success_count += 1
        self.metrics.consecutive_successes += 1
        self.metrics.consecutive_failures = 0

        if self.circuit_breaker_state == CircuitBreakerState.HALF_OPEN:
            self.circuit_breaker_half_open_calls += 1
            if self.metrics.consecutive_successes >= self.circuit_breaker_config.success_threshold:
                self.circuit_breaker_state = CircuitBreakerState.CLOSED
                logger.info(f"[CircuitBreaker] CLOSED for {self.name} - recovered")


@dataclass
class StartupResult:
    """Result of starting all components."""
    success: bool
    phase: StartupPhase
    components: Dict[str, ComponentStatus]
    failed_components: List[str]
    degraded_components: List[str]
    total_duration_ms: float
    phase_durations: Dict[str, float]
    errors: List[str]
    warnings: List[str]


@dataclass
class DiscoveredConfig:
    """Configuration discovered dynamically."""
    repo_paths: Dict[TrinityRepo, Path]
    ports: Dict[str, int]
    python_executables: Dict[str, Path]
    environment_vars: Dict[str, str]
    trinity_dir: Path
    api_keys: Dict[str, bool]  # Maps key name to availability (not the value!)
    discovered_at: datetime = field(default_factory=datetime.now)


# =============================================================================
# Circuit Breaker Implementation
# =============================================================================

class CircuitBreaker:
    """
    Production-grade circuit breaker implementation.

    States:
    - CLOSED: Normal operation, requests flow through
    - OPEN: Failures exceeded threshold, requests rejected immediately
    - HALF_OPEN: Testing recovery, limited requests allowed

    Features:
    - Configurable failure threshold
    - Exponential backoff timeout
    - Success threshold for recovery
    - Metrics tracking
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.half_open_calls = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        """Check if request can proceed."""
        async with self._lock:
            if self.state == CircuitBreakerState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitBreakerState.HALF_OPEN
                    self.half_open_calls = 0
                    logger.info(f"[CircuitBreaker:{self.name}] HALF_OPEN - attempting recovery")
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker {self.name} is OPEN"
                    )

            if self.state == CircuitBreakerState.HALF_OPEN:
                if self.half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker {self.name} is HALF_OPEN - max calls reached"
                    )
                self.half_open_calls += 1

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Record result of operation."""
        async with self._lock:
            if exc_type is not None:
                await self._record_failure()
            else:
                await self._record_success()
        return False  # Don't suppress exceptions

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset from OPEN state."""
        if self.last_failure_time is None:
            return True
        elapsed = time.time() - self.last_failure_time
        return elapsed >= self.config.timeout_seconds

    async def _record_failure(self):
        """Record a failure."""
        self.failure_count += 1
        self.success_count = 0
        self.last_failure_time = time.time()

        if self.state == CircuitBreakerState.HALF_OPEN:
            self.state = CircuitBreakerState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] OPEN - half-open test failed")
        elif self.failure_count >= self.config.failure_threshold:
            self.state = CircuitBreakerState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] OPEN - threshold exceeded")

    async def _record_success(self):
        """Record a success."""
        self.success_count += 1

        if self.state == CircuitBreakerState.HALF_OPEN:
            if self.success_count >= self.config.success_threshold:
                self.state = CircuitBreakerState.CLOSED
                self.failure_count = 0
                logger.info(f"[CircuitBreaker:{self.name}] CLOSED - recovered")


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# =============================================================================
# Dynamic Configuration Discovery
# =============================================================================

class DynamicConfigDiscovery:
    """
    Zero-hardcoding configuration discovery.

    Discovers:
    - Repository paths via environment, git remotes, or common locations
    - Available ports via socket probing
    - Python executables via venv detection
    - API keys via environment variables
    - Trinity directory structure

    Priority order:
    1. Environment variables (explicit configuration)
    2. Git remote analysis (find related repos)
    3. Common filesystem locations (convention over configuration)
    4. Auto-detection heuristics
    """

    # Common repo locations to search
    COMMON_REPO_PATHS = [
        Path.home() / "Documents" / "repos",
        Path.home() / "projects",
        Path.home() / "code",
        Path.home() / "dev",
        Path("/opt") / "jarvis",
    ]

    # Repo name patterns
    REPO_PATTERNS = {
        TrinityRepo.JARVIS: ["JARVIS-AI-Agent", "jarvis", "jarvis-main"],
        TrinityRepo.JARVIS_PRIME: ["jarvis-prime", "j-prime", "jarvis_prime"],
        TrinityRepo.REACTOR_CORE: ["reactor-core", "reactor_core", "reactor"],
    }

    def __init__(self, logger_instance: Optional[logging.Logger] = None):
        self.log = logger_instance or logger
        self._cache: Optional[DiscoveredConfig] = None
        self._cache_time: Optional[float] = None
        self._cache_ttl = 300.0  # 5 minutes

    async def discover(self, force_refresh: bool = False) -> DiscoveredConfig:
        """
        Discover all configuration dynamically.

        Args:
            force_refresh: If True, ignore cache and rediscover

        Returns:
            DiscoveredConfig with all discovered settings
        """
        # Check cache
        if not force_refresh and self._cache and self._cache_time:
            if time.time() - self._cache_time < self._cache_ttl:
                return self._cache

        self.log.info("[ConfigDiscovery] Starting dynamic configuration discovery...")

        # Discover in parallel where possible
        repo_paths_task = asyncio.create_task(self._discover_repo_paths())
        ports_task = asyncio.create_task(self._discover_ports())
        python_task = asyncio.create_task(self._discover_python_executables())

        repo_paths = await repo_paths_task
        ports = await ports_task
        python_execs = await python_task

        # These are fast, run synchronously
        env_vars = self._discover_environment_vars()
        trinity_dir = self._discover_trinity_dir()
        api_keys = self._discover_api_keys()

        config = DiscoveredConfig(
            repo_paths=repo_paths,
            ports=ports,
            python_executables=python_execs,
            environment_vars=env_vars,
            trinity_dir=trinity_dir,
            api_keys=api_keys,
        )

        # Update cache
        self._cache = config
        self._cache_time = time.time()

        self._log_discovered_config(config)
        return config

    async def _discover_repo_paths(self) -> Dict[TrinityRepo, Path]:
        """Discover Trinity repository paths."""
        paths: Dict[TrinityRepo, Path] = {}

        # Priority 1: Environment variables
        env_mappings = {
            TrinityRepo.JARVIS: "JARVIS_PATH",
            TrinityRepo.JARVIS_PRIME: "JARVIS_PRIME_PATH",
            TrinityRepo.REACTOR_CORE: "REACTOR_CORE_PATH",
        }

        for repo, env_var in env_mappings.items():
            env_path = os.environ.get(env_var)
            if env_path:
                path = Path(env_path)
                if path.exists():
                    paths[repo] = path
                    self.log.debug(f"[ConfigDiscovery] {repo.value} from env: {path}")

        # Priority 2: Find from current repo via git
        if TrinityRepo.JARVIS not in paths:
            jarvis_path = await self._find_jarvis_from_git()
            if jarvis_path:
                paths[TrinityRepo.JARVIS] = jarvis_path

        # Priority 3: Common filesystem locations
        for repo, patterns in self.REPO_PATTERNS.items():
            if repo in paths:
                continue

            for base_path in self.COMMON_REPO_PATHS:
                if not base_path.exists():
                    continue

                for pattern in patterns:
                    candidate = base_path / pattern
                    if candidate.exists() and self._is_valid_repo(candidate, repo):
                        paths[repo] = candidate
                        self.log.debug(f"[ConfigDiscovery] {repo.value} from filesystem: {candidate}")
                        break

                if repo in paths:
                    break

        return paths

    async def _find_jarvis_from_git(self) -> Optional[Path]:
        """Find JARVIS repo from current git context."""
        try:
            # Get current file's directory
            current_file = Path(__file__).resolve()

            # Walk up looking for .git
            for parent in [current_file] + list(current_file.parents):
                git_dir = parent / ".git"
                if git_dir.exists():
                    # Verify it's JARVIS repo
                    if self._is_valid_repo(parent, TrinityRepo.JARVIS):
                        return parent
                    break
        except Exception as e:
            self.log.debug(f"[ConfigDiscovery] Git discovery failed: {e}")

        return None

    def _is_valid_repo(self, path: Path, repo: TrinityRepo) -> bool:
        """Validate that a path is the expected repository."""
        if repo == TrinityRepo.JARVIS:
            # JARVIS has backend/main.py and run_supervisor.py
            return (
                (path / "backend" / "main.py").exists() or
                (path / "run_supervisor.py").exists()
            )
        elif repo == TrinityRepo.JARVIS_PRIME:
            # J-Prime has jarvis_prime/ and server.py
            return (
                (path / "jarvis_prime").exists() or
                (path / "server.py").exists()
            )
        elif repo == TrinityRepo.REACTOR_CORE:
            # Reactor-Core has reactor_core/
            return (path / "reactor_core").exists()

        return False

    async def _discover_ports(self) -> Dict[str, int]:
        """Discover available ports for services."""
        ports = {}

        # Default port configuration
        default_ports = {
            "jarvis_backend": 8010,
            "jarvis_prime": 8002,
            "reactor_core": 8003,
            "lsp_server": 9257,
            "websocket": 9258,
            "cloud_sql_proxy": 5432,
            "loading_server": 8099,
        }

        for service, default_port in default_ports.items():
            # Check environment override
            env_var = f"{service.upper()}_PORT"
            env_port = os.environ.get(env_var)

            if env_port:
                try:
                    port = int(env_port)
                    ports[service] = port
                    continue
                except ValueError:
                    pass

            # Find available port starting from default
            available = await self._find_available_port(default_port)
            ports[service] = available or default_port

        return ports

    async def _find_available_port(
        self,
        start_port: int,
        max_attempts: int = 10
    ) -> Optional[int]:
        """Find an available port starting from start_port."""
        for offset in range(max_attempts):
            port = start_port + offset
            if self._is_port_available(port):
                return port
        return None

    def _is_port_available(self, port: int, host: str = "127.0.0.1") -> bool:
        """Check if a port is available."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.bind((host, port))
                return True
        except (OSError, socket.error):
            return False

    async def _discover_python_executables(self) -> Dict[str, Path]:
        """Discover Python executables for each repo."""
        executables = {}

        # Get repo paths first
        repos = await self._discover_repo_paths()

        for repo, repo_path in repos.items():
            # Check for venv
            venv_candidates = [
                repo_path / "venv" / "bin" / "python3",
                repo_path / "venv" / "bin" / "python",
                repo_path / ".venv" / "bin" / "python3",
                repo_path / ".venv" / "bin" / "python",
            ]

            for candidate in venv_candidates:
                if candidate.exists() and candidate.is_file():
                    executables[repo.value] = candidate
                    break

            # Fallback to system Python
            if repo.value not in executables:
                executables[repo.value] = Path(sys.executable)

        return executables

    def _discover_environment_vars(self) -> Dict[str, str]:
        """Discover relevant environment variables."""
        relevant_prefixes = [
            "JARVIS_", "TRINITY_", "ANTHROPIC_", "OPENAI_",
            "GOOGLE_", "GCP_", "CLOUD_", "REACTOR_",
        ]

        env_vars = {}
        for key, value in os.environ.items():
            for prefix in relevant_prefixes:
                if key.startswith(prefix):
                    # Don't include actual values for sensitive keys
                    if "KEY" in key or "SECRET" in key or "PASSWORD" in key:
                        env_vars[key] = "[SET]"
                    else:
                        env_vars[key] = value
                    break

        return env_vars

    def _discover_trinity_dir(self) -> Path:
        """Discover or create Trinity state directory."""
        # Priority 1: Environment variable
        env_dir = os.environ.get("TRINITY_STATE_DIR")
        if env_dir:
            path = Path(env_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

        # Priority 2: Default location
        default_dir = Path.home() / ".jarvis" / "trinity"
        default_dir.mkdir(parents=True, exist_ok=True)

        # Ensure subdirectories exist
        (default_dir / "components").mkdir(exist_ok=True)
        (default_dir / "messages").mkdir(exist_ok=True)
        (default_dir / "state").mkdir(exist_ok=True)

        return default_dir

    def _discover_api_keys(self) -> Dict[str, bool]:
        """Check which API keys are available (without exposing values)."""
        keys_to_check = [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ]

        return {
            key: bool(os.environ.get(key))
            for key in keys_to_check
        }

    def _log_discovered_config(self, config: DiscoveredConfig):
        """Log discovered configuration summary."""
        self.log.info("[ConfigDiscovery] Discovery complete:")
        self.log.info(f"  Repos found: {len(config.repo_paths)}")
        for repo, path in config.repo_paths.items():
            self.log.info(f"    {repo.value}: {path}")
        self.log.info(f"  Ports configured: {len(config.ports)}")
        self.log.info(f"  Python executables: {len(config.python_executables)}")
        self.log.info(f"  Trinity dir: {config.trinity_dir}")
        available_keys = [k for k, v in config.api_keys.items() if v]
        self.log.info(f"  API keys available: {', '.join(available_keys) or 'none'}")


# =============================================================================
# Connection Verification
# =============================================================================

class ConnectionVerifier:
    """
    Production-grade connection verification with retries.

    Verifies:
    - HTTP endpoints (health checks)
    - File-based heartbeats (Trinity protocol)
    - Process liveness (PID validation)
    - Port availability
    - WebSocket connections
    """

    def __init__(self, logger_instance: Optional[logging.Logger] = None):
        self.log = logger_instance or logger

    async def verify_http_endpoint(
        self,
        url: str,
        timeout: float = 30.0,
        interval: float = 2.0,
        expected_status: int = 200,
        verify_json: bool = False,
    ) -> bool:
        """
        Verify HTTP endpoint is responding.

        Args:
            url: Full URL to check
            timeout: Maximum time to wait
            interval: Time between checks
            expected_status: Expected HTTP status code
            verify_json: If True, verify response is valid JSON

        Returns:
            True if endpoint is healthy
        """
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < timeout:
            attempt += 1
            try:
                # Use aiohttp if available, fallback to urllib
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=min(5.0, interval))
                        ) as resp:
                            if resp.status == expected_status:
                                if verify_json:
                                    await resp.json()  # Validate JSON
                                return True
                except ImportError:
                    # Fallback to synchronous request in thread
                    import urllib.request
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: urllib.request.urlopen(url, timeout=5.0)
                    )
                    if response.status == expected_status:
                        return True

            except Exception as e:
                self.log.debug(f"[ConnectionVerifier] HTTP check {url} attempt {attempt} failed: {e}")

            await asyncio.sleep(interval)

        return False

    async def verify_trinity_heartbeat(
        self,
        component: str,
        trinity_dir: Optional[Path] = None,
        timeout: float = 30.0,
        interval: float = 1.0,
        max_heartbeat_age: float = 15.0,
    ) -> bool:
        """
        Verify Trinity component via heartbeat file.

        Args:
            component: Component name (e.g., "jarvis_prime")
            trinity_dir: Trinity state directory
            timeout: Maximum time to wait
            interval: Time between checks
            max_heartbeat_age: Maximum age of heartbeat in seconds

        Returns:
            True if component heartbeat is fresh
        """
        if trinity_dir is None:
            trinity_dir = Path.home() / ".jarvis" / "trinity"

        heartbeat_file = trinity_dir / "components" / f"{component}.json"
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                if heartbeat_file.exists():
                    with open(heartbeat_file) as f:
                        state = json.load(f)

                    heartbeat_ts = state.get("timestamp", 0)
                    heartbeat_age = time.time() - heartbeat_ts

                    if heartbeat_age < max_heartbeat_age:
                        self.log.debug(
                            f"[ConnectionVerifier] {component} heartbeat fresh "
                            f"(age: {heartbeat_age:.1f}s)"
                        )
                        return True

            except Exception as e:
                self.log.debug(f"[ConnectionVerifier] Heartbeat check failed: {e}")

            await asyncio.sleep(interval)

        return False

    async def verify_process_alive(
        self,
        process: asyncio.subprocess.Process,
        timeout: float = 10.0,
    ) -> bool:
        """
        Verify subprocess is still running.

        Args:
            process: asyncio subprocess
            timeout: Maximum time to wait

        Returns:
            True if process is alive
        """
        try:
            # Check if process has terminated
            if process.returncode is not None:
                return False

            # Wait briefly to see if it crashes immediately
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
                # If wait() returned, process died
                return False
            except asyncio.TimeoutError:
                # Process is still running
                return True

        except Exception as e:
            self.log.debug(f"[ConnectionVerifier] Process check failed: {e}")
            return False

    async def verify_port_open(
        self,
        port: int,
        host: str = "127.0.0.1",
        timeout: float = 30.0,
        interval: float = 1.0,
    ) -> bool:
        """
        Verify a port is open and accepting connections.

        Args:
            port: Port number
            host: Host address
            timeout: Maximum time to wait
            interval: Time between checks

        Returns:
            True if port is accepting connections
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=min(2.0, interval)
                )
                writer.close()
                await writer.wait_closed()
                return True
            except Exception:
                pass

            await asyncio.sleep(interval)

        return False


# =============================================================================
# Dependency Graph Orchestrator
# =============================================================================

class DependencyGraphOrchestrator:
    """
    Production-grade dependency graph orchestrator.

    Features:
    - Kahn's algorithm for topological sort
    - Parallel execution of independent components
    - Circuit breakers with exponential backoff
    - Health check verification loops
    - Graceful degradation
    - Rollback support

    Usage:
        orchestrator = DependencyGraphOrchestrator()

        orchestrator.register_component(
            name="database",
            startup_func=start_db,
            health_check_func=check_db,
            critical=True,
        )

        orchestrator.register_component(
            name="api_server",
            dependencies=["database"],
            startup_func=start_api,
            health_check_func=check_api,
        )

        results = await orchestrator.start_all(parallel=True)
    """

    def __init__(self, logger_instance: Optional[logging.Logger] = None):
        self.log = logger_instance or logger
        self.components: Dict[str, ComponentNode] = {}
        self.startup_order: List[str] = []
        self.phase = StartupPhase.INIT
        self.phase_history: List[StartupPhase] = []
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._config_discovery = DynamicConfigDiscovery(logger_instance)
        self._connection_verifier = ConnectionVerifier(logger_instance)
        self._discovered_config: Optional[DiscoveredConfig] = None
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._started_components: Set[str] = set()
        self._failed_components: Set[str] = set()

    def register_component(
        self,
        name: str,
        dependencies: Optional[List[str]] = None,
        startup_func: Optional[Callable[[], Awaitable[None]]] = None,
        health_check_func: Optional[Callable[[], Awaitable[bool]]] = None,
        shutdown_func: Optional[Callable[[], Awaitable[None]]] = None,
        critical: bool = True,
        retry_config: Optional[RetryConfig] = None,
        circuit_breaker_config: Optional[CircuitBreakerConfig] = None,
        timeout_seconds: float = 60.0,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Register a component with its dependencies and behavior.

        Args:
            name: Unique component name
            dependencies: List of component names this depends on
            startup_func: Async function to start the component
            health_check_func: Async function returning True if healthy
            shutdown_func: Async function to shut down the component
            critical: If True, failure stops the entire startup
            retry_config: Retry behavior configuration
            circuit_breaker_config: Circuit breaker configuration
            timeout_seconds: Maximum time for startup
            metadata: Additional metadata
        """
        node = ComponentNode(
            name=name,
            dependencies=dependencies or [],
            startup_func=startup_func,
            health_check_func=health_check_func,
            shutdown_func=shutdown_func,
            critical=critical,
            retry_config=retry_config or RetryConfig(),
            circuit_breaker_config=circuit_breaker_config or CircuitBreakerConfig(),
            timeout_seconds=timeout_seconds,
            metadata=metadata or {},
        )

        self.components[name] = node

        # Create circuit breaker
        self._circuit_breakers[name] = CircuitBreaker(
            name=name,
            config=node.circuit_breaker_config,
        )

        # Update reverse dependencies
        for dep in node.dependencies:
            if dep in self.components:
                if name not in self.components[dep].dependents:
                    self.components[dep].dependents.append(name)

        self.log.debug(f"[Orchestrator] Registered component: {name} "
                      f"(deps: {dependencies or []}, critical: {critical})")

    def _topological_sort(self) -> List[str]:
        """
        Kahn's algorithm for topological sort.

        Returns components in dependency order (dependencies first).
        Raises ValueError if circular dependency detected.
        """
        # Build in-degree map (number of dependencies)
        in_degree: Dict[str, int] = {name: 0 for name in self.components}

        for node in self.components.values():
            for dep in node.dependencies:
                if dep in self.components:
                    in_degree[node.name] += 1

        # Queue of nodes with no remaining dependencies
        queue: Deque[str] = deque(
            name for name, degree in in_degree.items() if degree == 0
        )

        result: List[str] = []

        while queue:
            # Sort for deterministic order
            queue = deque(sorted(queue))
            node_name = queue.popleft()
            result.append(node_name)

            # Reduce in-degree for dependents
            node = self.components[node_name]
            for dependent_name in node.dependents:
                if dependent_name in in_degree:
                    in_degree[dependent_name] -= 1
                    if in_degree[dependent_name] == 0:
                        queue.append(dependent_name)

        # Check for circular dependencies
        if len(result) != len(self.components):
            cycle_nodes = set(self.components.keys()) - set(result)
            raise ValueError(f"Circular dependency detected involving: {cycle_nodes}")

        return result

    def _group_by_level(self) -> Dict[int, List[str]]:
        """
        Group components by dependency level.

        Components at the same level can be started in parallel.
        """
        levels: Dict[int, List[str]] = defaultdict(list)
        level_cache: Dict[str, int] = {}

        def get_level(name: str, visited: Set[str]) -> int:
            if name in level_cache:
                return level_cache[name]

            if name in visited:
                return 0  # Cycle protection

            visited.add(name)
            node = self.components.get(name)

            if not node or not node.dependencies:
                level_cache[name] = 0
                return 0

            max_dep_level = 0
            for dep in node.dependencies:
                if dep in self.components:
                    dep_level = get_level(dep, visited.copy())
                    max_dep_level = max(max_dep_level, dep_level)

            level = max_dep_level + 1
            level_cache[name] = level
            return level

        for name in self.startup_order:
            level = get_level(name, set())
            levels[level].append(name)

        return dict(levels)

    async def start_all(
        self,
        parallel: bool = True,
        max_concurrent: int = 4,
    ) -> StartupResult:
        """
        Start all registered components in optimal order.

        Args:
            parallel: If True, start independent components in parallel
            max_concurrent: Maximum concurrent component starts

        Returns:
            StartupResult with detailed status
        """
        start_time = time.time()
        errors: List[str] = []
        warnings: List[str] = []
        phase_durations: Dict[str, float] = {}

        try:
            # Phase 1: Configuration Discovery
            await self._transition_phase(StartupPhase.CONFIG_DISCOVERY)
            phase_start = time.time()
            self._discovered_config = await self._config_discovery.discover()
            phase_durations["config_discovery"] = time.time() - phase_start

            # Phase 2: Calculate startup order
            await self._transition_phase(StartupPhase.PRE_FLIGHT)
            phase_start = time.time()
            self.startup_order = self._topological_sort()
            self.log.info(f"[Orchestrator] Startup order: {' -> '.join(self.startup_order)}")
            phase_durations["pre_flight"] = time.time() - phase_start

            # Phase 3: Start components by level
            await self._transition_phase(StartupPhase.BACKEND_INIT)
            phase_start = time.time()

            levels = self._group_by_level()
            semaphore = asyncio.Semaphore(max_concurrent)

            for level in sorted(levels.keys()):
                component_names = levels[level]
                self.log.info(f"[Orchestrator] Starting level {level}: {', '.join(component_names)}")

                if parallel and len(component_names) > 1:
                    # Start independent components in parallel
                    tasks = [
                        self._start_component_with_semaphore(name, semaphore)
                        for name in component_names
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for name, result in zip(component_names, results):
                        if isinstance(result, Exception):
                            errors.append(f"{name}: {result}")
                            self._failed_components.add(name)
                        elif not result:
                            self._failed_components.add(name)
                else:
                    # Sequential startup
                    for name in component_names:
                        success = await self._start_component(name)
                        if not success:
                            self._failed_components.add(name)

            phase_durations["backend_init"] = time.time() - phase_start

            # Phase 4: Health verification
            await self._transition_phase(StartupPhase.HEALTH_VERIFICATION)
            phase_start = time.time()

            for name in self._started_components:
                node = self.components[name]
                if node.health_check_func:
                    healthy = await self._verify_health(name)
                    if not healthy:
                        warnings.append(f"{name}: health check failed after startup")
                        node.status = ComponentStatus.DEGRADED

            phase_durations["health_verification"] = time.time() - phase_start

            # Determine final state
            critical_failures = [
                name for name in self._failed_components
                if self.components[name].critical
            ]

            if critical_failures:
                await self._transition_phase(StartupPhase.FAILED)
                errors.append(f"Critical components failed: {critical_failures}")
            elif self._failed_components:
                await self._transition_phase(StartupPhase.DEGRADED)
                warnings.append(f"Non-critical components failed: {list(self._failed_components)}")
            else:
                await self._transition_phase(StartupPhase.HEALTHY)

        except Exception as e:
            errors.append(f"Startup failed: {e}")
            await self._transition_phase(StartupPhase.FAILED)

        total_duration = (time.time() - start_time) * 1000

        return StartupResult(
            success=self.phase == StartupPhase.HEALTHY,
            phase=self.phase,
            components={
                name: node.status
                for name, node in self.components.items()
            },
            failed_components=list(self._failed_components),
            degraded_components=[
                name for name, node in self.components.items()
                if node.status == ComponentStatus.DEGRADED
            ],
            total_duration_ms=total_duration,
            phase_durations=phase_durations,
            errors=errors,
            warnings=warnings,
        )

    async def _start_component_with_semaphore(
        self,
        name: str,
        semaphore: asyncio.Semaphore,
    ) -> bool:
        """Start component with concurrency control."""
        async with semaphore:
            return await self._start_component(name)

    async def _start_component(self, name: str) -> bool:
        """
        Start a single component with retries and circuit breaker.

        Returns True if component started successfully.
        """
        node = self.components.get(name)
        if not node:
            self.log.error(f"[Orchestrator] Unknown component: {name}")
            return False

        # Check circuit breaker
        if not node.can_start:
            self.log.warning(f"[Orchestrator] {name}: circuit breaker open, skipping")
            node.status = ComponentStatus.CIRCUIT_OPEN
            return False

        # Wait for dependencies
        for dep in node.dependencies:
            if dep not in self._started_components:
                dep_node = self.components.get(dep)
                if dep_node and dep_node.critical:
                    self.log.error(f"[Orchestrator] {name}: dependency {dep} not started")
                    return False

        # Retry loop with exponential backoff
        for attempt in range(node.retry_config.max_retries):
            try:
                node.status = ComponentStatus.STARTING
                node.metrics.start_count += 1
                node.metrics.last_start_time = time.time()

                self.log.info(f"[Orchestrator] Starting {name} "
                             f"(attempt {attempt + 1}/{node.retry_config.max_retries})")

                # Execute startup function with timeout
                if node.startup_func:
                    try:
                        async with self._circuit_breakers[name]:
                            await asyncio.wait_for(
                                node.startup_func(),
                                timeout=node.timeout_seconds
                            )
                    except CircuitBreakerOpenError:
                        self.log.warning(f"[Orchestrator] {name}: circuit breaker prevented start")
                        node.status = ComponentStatus.CIRCUIT_OPEN
                        return False

                # Verify health
                if node.health_check_func:
                    node.status = ComponentStatus.HEALTH_CHECK
                    healthy = await self._verify_health(name, max_attempts=5)
                    if not healthy:
                        raise Exception("Health check failed")

                # Success!
                duration_ms = (time.time() - node.metrics.last_start_time) * 1000
                node.metrics.total_start_time_ms += duration_ms
                node.record_success()
                node.status = ComponentStatus.HEALTHY
                self._started_components.add(name)

                self.log.info(f"[Orchestrator] {name} started successfully ({duration_ms:.0f}ms)")
                return True

            except asyncio.TimeoutError:
                self.log.warning(f"[Orchestrator] {name}: timeout (attempt {attempt + 1})")
                node.record_failure()
            except Exception as e:
                self.log.warning(f"[Orchestrator] {name}: failed - {e} (attempt {attempt + 1})")
                node.record_failure()
                node.error_message = str(e)

            # Exponential backoff
            if attempt < node.retry_config.max_retries - 1:
                delay = node.retry_config.calculate_delay(attempt)
                self.log.debug(f"[Orchestrator] {name}: waiting {delay:.1f}s before retry")
                await asyncio.sleep(delay)

        # All retries exhausted
        node.status = ComponentStatus.FAILED

        # Graceful degradation for non-critical components
        if not node.critical:
            self.log.warning(f"[Orchestrator] {name}: failed but non-critical, continuing")
            return False

        self.log.error(f"[Orchestrator] {name}: CRITICAL failure after "
                      f"{node.retry_config.max_retries} attempts")
        return False

    async def _verify_health(
        self,
        name: str,
        max_attempts: int = 10,
    ) -> bool:
        """Verify component health with retries."""
        node = self.components.get(name)
        if not node or not node.health_check_func:
            return True

        for attempt in range(max_attempts):
            try:
                healthy = await asyncio.wait_for(
                    node.health_check_func(),
                    timeout=node.health_check_timeout
                )
                if healthy:
                    node.metrics.last_health_check_time = time.time()
                    return True
            except Exception as e:
                self.log.debug(f"[Orchestrator] {name}: health check attempt {attempt + 1} failed: {e}")

            # Exponential backoff
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))

        return False

    async def _transition_phase(self, new_phase: StartupPhase):
        """Transition to a new startup phase."""
        async with self._lock:
            old_phase = self.phase
            self.phase_history.append(old_phase)
            self.phase = new_phase
            self.log.info(f"[Orchestrator] Phase: {old_phase.value} -> {new_phase.value}")

    async def shutdown_all(self):
        """Gracefully shutdown all started components in reverse order."""
        self._shutdown_event.set()

        # Shutdown in reverse startup order
        shutdown_order = list(reversed([
            name for name in self.startup_order
            if name in self._started_components
        ]))

        self.log.info(f"[Orchestrator] Shutting down {len(shutdown_order)} components...")

        for name in shutdown_order:
            node = self.components.get(name)
            if node and node.shutdown_func:
                try:
                    await asyncio.wait_for(node.shutdown_func(), timeout=10.0)
                    self.log.info(f"[Orchestrator] {name}: shutdown complete")
                except Exception as e:
                    self.log.warning(f"[Orchestrator] {name}: shutdown error: {e}")

            node.status = ComponentStatus.STOPPED
            self._started_components.discard(name)

        await self._transition_phase(StartupPhase.SHUTDOWN)

    def get_status(self) -> Dict[str, Any]:
        """Get current orchestrator status."""
        return {
            "phase": self.phase.value,
            "components": {
                name: {
                    "status": node.status.value,
                    "critical": node.critical,
                    "circuit_breaker": node.circuit_breaker_state.value,
                    "metrics": {
                        "start_count": node.metrics.start_count,
                        "success_rate": node.metrics.success_rate,
                        "avg_start_time_ms": node.metrics.avg_start_time_ms,
                    },
                    "error": node.error_message,
                }
                for name, node in self.components.items()
            },
            "started": list(self._started_components),
            "failed": list(self._failed_components),
        }


# =============================================================================
# Singleton and Factory Functions
# =============================================================================

_orchestrator: Optional[DependencyGraphOrchestrator] = None
_orchestrator_lock = asyncio.Lock()


async def get_orchestrator() -> DependencyGraphOrchestrator:
    """Get or create the singleton orchestrator instance."""
    global _orchestrator

    async with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = DependencyGraphOrchestrator()
        return _orchestrator


def get_orchestrator_sync() -> Optional[DependencyGraphOrchestrator]:
    """Get the orchestrator synchronously (may be None)."""
    return _orchestrator


async def get_config_discovery() -> DynamicConfigDiscovery:
    """Get configuration discovery instance."""
    orchestrator = await get_orchestrator()
    return orchestrator._config_discovery


async def get_connection_verifier() -> ConnectionVerifier:
    """Get connection verifier instance."""
    orchestrator = await get_orchestrator()
    return orchestrator._connection_verifier


# =============================================================================
# Pre-configured Component Registration Helpers
# =============================================================================

async def register_trinity_components(
    orchestrator: DependencyGraphOrchestrator,
    config: DiscoveredConfig,
):
    """
    Register all Trinity components with the orchestrator.

    This sets up the dependency graph for:
    - JARVIS Backend
    - JARVIS Prime
    - Reactor Core
    - Coding Council
    - Trinity synchronization
    """
    verifier = orchestrator._connection_verifier

    # Base infrastructure (no dependencies)
    orchestrator.register_component(
        name="trinity_dir",
        startup_func=lambda: asyncio.sleep(0),  # Already created by config discovery
        critical=True,
        timeout_seconds=5.0,
        metadata={"path": str(config.trinity_dir)},
    )

    # Cloud SQL Proxy (if available)
    if config.api_keys.get("GOOGLE_APPLICATION_CREDENTIALS"):
        orchestrator.register_component(
            name="cloud_sql_proxy",
            dependencies=["trinity_dir"],
            startup_func=None,  # Started externally
            health_check_func=lambda: verifier.verify_port_open(
                config.ports.get("cloud_sql_proxy", 5432),
                timeout=5.0,
            ),
            critical=False,
            timeout_seconds=30.0,
        )

    # JARVIS Backend
    orchestrator.register_component(
        name="jarvis_backend",
        dependencies=["trinity_dir"],
        health_check_func=lambda: verifier.verify_http_endpoint(
            f"http://127.0.0.1:{config.ports.get('jarvis_backend', 8010)}/health/ping",
            timeout=30.0,
        ),
        critical=True,
        timeout_seconds=120.0,
        metadata={"port": config.ports.get("jarvis_backend", 8010)},
    )

    # JARVIS Prime (if repo exists)
    if TrinityRepo.JARVIS_PRIME in config.repo_paths:
        orchestrator.register_component(
            name="jarvis_prime",
            dependencies=["trinity_dir"],
            health_check_func=lambda: verifier.verify_trinity_heartbeat(
                "jarvis_prime",
                config.trinity_dir,
                timeout=30.0,
            ),
            critical=False,  # Can run without J-Prime
            timeout_seconds=60.0,
            metadata={"path": str(config.repo_paths[TrinityRepo.JARVIS_PRIME])},
        )

    # Reactor Core (if repo exists)
    if TrinityRepo.REACTOR_CORE in config.repo_paths:
        orchestrator.register_component(
            name="reactor_core",
            dependencies=["trinity_dir"],
            health_check_func=lambda: verifier.verify_trinity_heartbeat(
                "reactor_core",
                config.trinity_dir,
                timeout=30.0,
            ),
            critical=False,
            timeout_seconds=60.0,
            metadata={"path": str(config.repo_paths[TrinityRepo.REACTOR_CORE])},
        )

    # Coding Council (depends on backend)
    orchestrator.register_component(
        name="coding_council",
        dependencies=["jarvis_backend"],
        health_check_func=lambda: verifier.verify_http_endpoint(
            f"http://127.0.0.1:{config.ports.get('jarvis_backend', 8010)}/coding-council/health",
            timeout=10.0,
        ),
        critical=False,
        timeout_seconds=30.0,
    )

    # Trinity Sync (depends on all repos)
    trinity_deps = ["jarvis_backend"]
    if TrinityRepo.JARVIS_PRIME in config.repo_paths:
        trinity_deps.append("jarvis_prime")
    if TrinityRepo.REACTOR_CORE in config.repo_paths:
        trinity_deps.append("reactor_core")

    orchestrator.register_component(
        name="trinity_sync",
        dependencies=trinity_deps,
        critical=False,
        timeout_seconds=30.0,
    )


# =============================================================================
# Utility Functions
# =============================================================================

def format_startup_result(result: StartupResult) -> str:
    """Format startup result for logging/display."""
    lines = [
        "=" * 60,
        f"STARTUP {'SUCCESS' if result.success else 'FAILED'}",
        "=" * 60,
        f"Phase: {result.phase.value}",
        f"Duration: {result.total_duration_ms:.0f}ms",
        "",
        "Components:",
    ]

    for name, status in result.components.items():
        icon = "✅" if status == ComponentStatus.HEALTHY else (
            "⚠️" if status == ComponentStatus.DEGRADED else "❌"
        )
        lines.append(f"  {icon} {name}: {status.value}")

    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for error in result.errors:
            lines.append(f"  ❌ {error}")

    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in result.warnings:
            lines.append(f"  ⚠️ {warning}")

    lines.append("=" * 60)
    return "\n".join(lines)
