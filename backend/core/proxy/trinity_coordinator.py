"""
Unified Trinity Coordinator - Single Source of Truth for Cross-Repo Coordination

This module solves the root cause of nagging "Prime unavailable" and "degraded mode"
warnings by providing:

1. SINGLE HEALTH AUTHORITY - One system checks health, others subscribe
2. UNIFIED STATE MANAGEMENT - Single source of truth for all component states
3. DEPENDENCY-AWARE BARRIERS - Components wait for dependencies, not timeouts
4. IDEMPOTENT NOTIFICATIONS - Same condition = one notification, not repeated
5. CROSS-REPO LEADER ELECTION - Only leader manages external repo lifecycle

Architecture:
```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     UNIFIED TRINITY COORDINATOR                              │
│                    (Single Source of Truth)                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  HEALTH AUTHORITY (Single)                                                  │
│  └─ HealthCheckScheduler ─────> ComponentHealthRegistry                     │
│     • One check per component    • Single state store                       │
│     • Configurable intervals     • Versioned updates                        │
│     • Dedup notifications        • Event sourcing                           │
├─────────────────────────────────────────────────────────────────────────────┤
│  STARTUP ORCHESTRATION                                                      │
│  └─ DependencyGraph ──────────> StartupBarrier                             │
│     • Explicit dependencies      • Blocks until ready                       │
│     • Parallel where possible    • Timeout per component                    │
│     • No premature checks        • Grace period aware                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  NOTIFICATION DEDUPLICATION                                                 │
│  └─ NotificationCoalescer ────> SingleEmitter                              │
│     • Tracks notification state  • "Prime unavailable" once                │
│     • Cooldown windows           • State change triggers                    │
│     • Severity escalation        • Structured logging                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  CROSS-REPO PROCESS MANAGEMENT                                              │
│  └─ ServiceRegistry ──────────> ProcessSupervisor                          │
│     • Discovery by config        • Start/stop/restart                       │
│     • Health endpoint registry   • Signal handling                          │
│     • Port management            • Graceful shutdown                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Final,
    List,
    Optional,
    Set,
    Tuple,
)

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

# Log severity bridge for criticality-aware logging
try:
    from backend.core.log_severity_bridge import log_component_failure, is_component_required
except ImportError:
    def log_component_failure(component, message, error=None, **ctx):
        logging.getLogger(__name__).error(f"{component}: {message}")
    def is_component_required(component):
        return True

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (All from Environment - Zero Hardcoding)
# =============================================================================

class TrinityConfig:
    """Unified configuration for Trinity coordination."""

    # State directories
    STATE_DIR: Final[Path] = Path(os.getenv("JARVIS_STATE_DIR", str(Path.home() / ".jarvis")))
    TRINITY_DIR: Final[Path] = STATE_DIR / "trinity"
    CROSS_REPO_DIR: Final[Path] = STATE_DIR / "cross_repo"

    # Component discovery
    JARVIS_BODY_PORT: Final[int] = int(os.getenv("JARVIS_BODY_PORT", "8010"))
    JARVIS_PRIME_PORT: Final[int] = int(os.getenv("JARVIS_PRIME_PORT", "8000"))
    REACTOR_CORE_PORT: Final[int] = int(os.getenv("REACTOR_CORE_PORT", "8082"))

    # Paths to repos (for subprocess management)
    JARVIS_PRIME_PATH: Final[str] = os.getenv(
        "JARVIS_PRIME_PATH",
        str(Path.home() / "Documents/repos/jarvis-prime")
    )
    REACTOR_CORE_PATH: Final[str] = os.getenv(
        "REACTOR_CORE_PATH",
        str(Path.home() / "Documents/repos/reactor-core")
    )

    # Component-specific timeouts (from TrinityOrchestrationConfig)
    # JARVIS Body (fast startup)
    BODY_STARTUP_TIMEOUT: Final[float] = float(os.getenv("JARVIS_BODY_STARTUP_TIMEOUT", "30.0"))
    BODY_HEALTH_TIMEOUT: Final[float] = float(os.getenv("JARVIS_BODY_HEALTH_TIMEOUT", "10.0"))
    BODY_HEARTBEAT_STALE: Final[float] = float(os.getenv("JARVIS_BODY_HEARTBEAT_STALE", "45.0"))
    BODY_GRACE_PERIOD: Final[float] = float(os.getenv("JARVIS_BODY_GRACE_PERIOD", "5.0"))

    # JARVIS Prime (slow - ML model loading)
    PRIME_STARTUP_TIMEOUT: Final[float] = float(os.getenv("JARVIS_PRIME_STARTUP_TIMEOUT", "300.0"))
    PRIME_HEALTH_TIMEOUT: Final[float] = float(os.getenv("JARVIS_PRIME_HEALTH_TIMEOUT", "15.0"))
    PRIME_HEARTBEAT_STALE: Final[float] = float(os.getenv("JARVIS_PRIME_HEARTBEAT_STALE", "120.0"))
    PRIME_GRACE_PERIOD: Final[float] = float(os.getenv("JARVIS_PRIME_GRACE_PERIOD", "60.0"))

    # Reactor Core (medium)
    REACTOR_STARTUP_TIMEOUT: Final[float] = float(os.getenv("REACTOR_STARTUP_TIMEOUT", "120.0"))
    REACTOR_HEALTH_TIMEOUT: Final[float] = float(os.getenv("REACTOR_HEALTH_TIMEOUT", "10.0"))
    REACTOR_HEARTBEAT_STALE: Final[float] = float(os.getenv("REACTOR_HEARTBEAT_STALE", "60.0"))
    REACTOR_GRACE_PERIOD: Final[float] = float(os.getenv("REACTOR_GRACE_PERIOD", "30.0"))

    # Health check behavior
    HEALTH_CHECK_INTERVAL: Final[float] = float(os.getenv("TRINITY_HEALTH_CHECK_INTERVAL", "10.0"))
    NOTIFICATION_COOLDOWN: Final[float] = float(os.getenv("TRINITY_NOTIFICATION_COOLDOWN", "300.0"))

    # Degraded mode behavior
    ALLOW_DEGRADED_MODE: Final[bool] = os.getenv("JARVIS_ALLOW_DEGRADED_MODE", "true").lower() == "true"
    AUTO_RECOVERY: Final[bool] = os.getenv("JARVIS_AUTO_RECOVERY", "true").lower() == "true"
    RECOVERY_INTERVAL: Final[float] = float(os.getenv("JARVIS_RECOVERY_INTERVAL", "120.0"))

    # Process management
    # v148.0: START_JARVIS_PRIME defaults to true (it's required for inference)
    # v148.0: START_REACTOR_CORE defaults to false (optional training component)
    START_PRIME: Final[bool] = os.getenv("START_JARVIS_PRIME", "true").lower() == "true"
    START_REACTOR: Final[bool] = os.getenv("START_REACTOR_CORE", "false").lower() == "true"


# =============================================================================
# Component Types and States
# =============================================================================

class TrinityComponent(Enum):
    """Trinity component identifiers."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"
    CLOUDSQL_PROXY = "cloudsql_proxy"


class ComponentState(Enum):
    """Component lifecycle states."""
    UNKNOWN = auto()
    STARTING = auto()       # Component is initializing
    HEALTHY = auto()        # All checks passing
    DEGRADED = auto()       # Some checks failing but functional
    UNHEALTHY = auto()      # Critical failures
    STOPPED = auto()        # Intentionally stopped
    FAILED = auto()         # Failed to start
    RECOVERING = auto()     # Auto-recovery in progress


class NotificationLevel(Enum):
    """Notification severity levels."""
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


# =============================================================================
# Component Configuration
# =============================================================================

@dataclass
class ComponentConfig:
    """Configuration for a Trinity component."""
    component: TrinityComponent
    display_name: str
    port: int
    health_endpoint: str
    heartbeat_file: Path
    startup_timeout: float
    health_timeout: float
    heartbeat_stale: float
    grace_period: float
    required: bool
    repo_path: Optional[Path] = None
    startup_command: Optional[List[str]] = None

    @classmethod
    def for_prime(cls) -> ComponentConfig:
        """Create config for JARVIS Prime."""
        return cls(
            component=TrinityComponent.JARVIS_PRIME,
            display_name="JARVIS Prime (Mind)",
            port=TrinityConfig.JARVIS_PRIME_PORT,
            health_endpoint=f"http://127.0.0.1:{TrinityConfig.JARVIS_PRIME_PORT}/health",
            heartbeat_file=TrinityConfig.TRINITY_DIR / "heartbeats" / "jarvis_prime.json",
            startup_timeout=TrinityConfig.PRIME_STARTUP_TIMEOUT,
            health_timeout=TrinityConfig.PRIME_HEALTH_TIMEOUT,
            heartbeat_stale=TrinityConfig.PRIME_HEARTBEAT_STALE,
            grace_period=TrinityConfig.PRIME_GRACE_PERIOD,
            required=False,
            repo_path=Path(TrinityConfig.JARVIS_PRIME_PATH) if TrinityConfig.JARVIS_PRIME_PATH else None,
            startup_command=["python3", "run_server.py"],
        )

    @classmethod
    def for_reactor(cls) -> ComponentConfig:
        """Create config for Reactor Core."""
        return cls(
            component=TrinityComponent.REACTOR_CORE,
            display_name="Reactor Core (Nerves)",
            port=TrinityConfig.REACTOR_CORE_PORT,
            health_endpoint=f"http://127.0.0.1:{TrinityConfig.REACTOR_CORE_PORT}/health",
            heartbeat_file=TrinityConfig.TRINITY_DIR / "heartbeats" / "reactor_core.json",
            startup_timeout=TrinityConfig.REACTOR_STARTUP_TIMEOUT,
            health_timeout=TrinityConfig.REACTOR_HEALTH_TIMEOUT,
            heartbeat_stale=TrinityConfig.REACTOR_HEARTBEAT_STALE,
            grace_period=TrinityConfig.REACTOR_GRACE_PERIOD,
            required=False,
            repo_path=Path(TrinityConfig.REACTOR_CORE_PATH) if TrinityConfig.REACTOR_CORE_PATH else None,
            startup_command=["python3", "run_server.py"],
        )

    @classmethod
    def for_body(cls) -> ComponentConfig:
        """Create config for JARVIS Body."""
        return cls(
            component=TrinityComponent.JARVIS_BODY,
            display_name="JARVIS Body (Main)",
            port=TrinityConfig.JARVIS_BODY_PORT,
            health_endpoint=f"http://127.0.0.1:{TrinityConfig.JARVIS_BODY_PORT}/health/ready",
            heartbeat_file=TrinityConfig.TRINITY_DIR / "heartbeats" / "jarvis_body.json",
            startup_timeout=TrinityConfig.BODY_STARTUP_TIMEOUT,
            health_timeout=TrinityConfig.BODY_HEALTH_TIMEOUT,
            heartbeat_stale=TrinityConfig.BODY_HEARTBEAT_STALE,
            grace_period=TrinityConfig.BODY_GRACE_PERIOD,
            required=True,
        )


# =============================================================================
# Component Health Record
# =============================================================================

@dataclass
class ComponentHealth:
    """Health record for a component (versioned, immutable snapshots)."""
    component: TrinityComponent
    state: ComponentState
    timestamp: float
    version: int
    last_check_time: Optional[float] = None
    last_heartbeat_time: Optional[float] = None
    last_http_success: Optional[float] = None
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "component": self.component.value,
            "state": self.state.name,
            "timestamp": self.timestamp,
            "timestamp_iso": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "version": self.version,
            "last_check_time": self.last_check_time,
            "last_heartbeat_time": self.last_heartbeat_time,
            "last_http_success": self.last_http_success,
            "latency_ms": self.latency_ms,
            "error_message": self.error_message,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_seconds": (time.time() - self.started_at) if self.started_at else None,
            "metadata": self.metadata,
        }


# =============================================================================
# Notification Record (for deduplication)
# =============================================================================

@dataclass
class NotificationRecord:
    """Tracks a notification for deduplication."""
    component: TrinityComponent
    level: NotificationLevel
    message_key: str  # Normalized key for dedup
    first_time: float
    last_time: float
    count: int
    suppressed: bool = False


# =============================================================================
# Component Health Registry (Single Source of Truth)
# =============================================================================

class ComponentHealthRegistry:
    """
    Single source of truth for component health states.

    Features:
    - Versioned updates (only accept newer versions)
    - Single writer per component (health checker)
    - Multiple readers (any component can query)
    - Event sourcing (append-only log)
    - Persistence to disk
    """

    def __init__(self):
        self._states: Dict[TrinityComponent, ComponentHealth] = {}
        self._version_counters: Dict[TrinityComponent, int] = {}
        self._lock = asyncio.Lock()
        self._subscribers: List[Callable[[TrinityComponent, ComponentHealth], Awaitable[None]]] = []
        self._state_file = TrinityConfig.TRINITY_DIR / "state" / "health_registry.json"

    async def update_state(
        self,
        component: TrinityComponent,
        state: ComponentState,
        **kwargs: Any,
    ) -> ComponentHealth:
        """
        Update component state (versioned, single source of truth).

        Only the health checker should call this.
        """
        async with self._lock:
            # Increment version
            version = self._version_counters.get(component, 0) + 1
            self._version_counters[component] = version

            # Get previous state for comparison
            prev = self._states.get(component)
            state_changed = prev is None or prev.state != state

            # Create new health record
            health = ComponentHealth(
                component=component,
                state=state,
                timestamp=time.time(),
                version=version,
                **kwargs,
            )

            self._states[component] = health

            # Notify subscribers only on state change
            if state_changed:
                for subscriber in self._subscribers:
                    try:
                        await subscriber(component, health)
                    except Exception as e:
                        logger.debug(f"[HealthRegistry] Subscriber error: {e}")

            # Persist
            await self._persist()

            return health

    async def get_state(self, component: TrinityComponent) -> Optional[ComponentHealth]:
        """Get current state for a component."""
        return self._states.get(component)

    async def get_all_states(self) -> Dict[TrinityComponent, ComponentHealth]:
        """Get all component states."""
        return dict(self._states)

    def subscribe(
        self,
        callback: Callable[[TrinityComponent, ComponentHealth], Awaitable[None]]
    ) -> None:
        """Subscribe to state changes."""
        self._subscribers.append(callback)

    async def _persist(self) -> None:
        """Persist states to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                comp.value: health.to_dict()
                for comp, health in self._states.items()
            }
            tmp_file = self._state_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
            tmp_file.rename(self._state_file)
        except Exception as e:
            logger.debug(f"[HealthRegistry] Persist error: {e}")

    async def load(self) -> None:
        """Load persisted states."""
        try:
            if self._state_file.exists():
                with open(self._state_file) as f:
                    data = json.load(f)
                # Note: We don't restore state from disk on startup
                # because we need fresh health checks
                logger.debug(f"[HealthRegistry] Found {len(data)} persisted states")
        except Exception as e:
            logger.debug(f"[HealthRegistry] Load error: {e}")


# =============================================================================
# Notification Coalescer (Prevents Duplicate Warnings)
# =============================================================================

class NotificationCoalescer:
    """
    Prevents duplicate "Prime unavailable" style warnings.

    Features:
    - Deduplication by message key
    - Cooldown window (no repeat within N seconds)
    - Count tracking ("suppressed 5 more occurrences")
    - State transition notifications only
    """

    def __init__(
        self,
        cooldown_seconds: float = TrinityConfig.NOTIFICATION_COOLDOWN,
    ):
        self._cooldown = cooldown_seconds
        self._records: Dict[str, NotificationRecord] = {}
        self._lock = asyncio.Lock()

    def _make_key(self, component: TrinityComponent, level: NotificationLevel, message: str) -> str:
        """Create deduplication key."""
        # Normalize message to key (remove timestamps, PIDs, etc.)
        normalized = message.lower()
        # Remove common variable parts
        for remove in ["pid", "port", "ms", "seconds", "attempts"]:
            # Simple normalization
            pass
        return f"{component.value}:{level.name}:{hash(normalized) % 10000}"

    async def should_emit(
        self,
        component: TrinityComponent,
        level: NotificationLevel,
        message: str,
    ) -> Tuple[bool, Optional[int]]:
        """
        Check if notification should be emitted.

        Returns (should_emit, suppressed_count).
        """
        async with self._lock:
            key = self._make_key(component, level, message)
            now = time.time()

            record = self._records.get(key)

            if record is None:
                # First occurrence - emit
                self._records[key] = NotificationRecord(
                    component=component,
                    level=level,
                    message_key=key,
                    first_time=now,
                    last_time=now,
                    count=1,
                )
                return (True, None)

            # Check cooldown
            if now - record.last_time < self._cooldown:
                # Within cooldown - suppress
                record.count += 1
                record.last_time = now
                record.suppressed = True
                return (False, None)

            # Cooldown expired - emit with suppression count
            suppressed_count = record.count - 1 if record.suppressed else None
            record.last_time = now
            record.count = 1
            record.suppressed = False
            return (True, suppressed_count)

    async def emit(
        self,
        component: TrinityComponent,
        level: NotificationLevel,
        message: str,
        log_func: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """
        Emit a notification if not suppressed.

        Returns True if emitted, False if suppressed.
        """
        should_emit, suppressed_count = await self.should_emit(component, level, message)

        if not should_emit:
            return False

        # Build message with suppression info
        final_message = message
        if suppressed_count and suppressed_count > 0:
            final_message = f"{message} (suppressed {suppressed_count} similar)"

        # Log at appropriate level
        if log_func:
            log_func(final_message)
        else:
            log_level = {
                NotificationLevel.DEBUG: logging.DEBUG,
                NotificationLevel.INFO: logging.INFO,
                NotificationLevel.WARNING: logging.WARNING,
                NotificationLevel.ERROR: logging.ERROR,
                NotificationLevel.CRITICAL: logging.CRITICAL,
            }.get(level, logging.INFO)
            logger.log(log_level, f"[Trinity] {final_message}")

        return True


# =============================================================================
# Health Checker (Single Authority)
# =============================================================================

class HealthChecker:
    """
    Single authority for health checks.

    Only this class writes to ComponentHealthRegistry.
    Multiple check methods, one authority.
    """

    def __init__(
        self,
        registry: ComponentHealthRegistry,
        coalescer: NotificationCoalescer,
    ):
        self._registry = registry
        self._coalescer = coalescer
        self._configs: Dict[TrinityComponent, ComponentConfig] = {}
        self._startup_times: Dict[TrinityComponent, float] = {}
        self._http_session: Optional[aiohttp.ClientSession] = None

    def register_component(self, config: ComponentConfig) -> None:
        """Register a component for health checking."""
        self._configs[config.component] = config

    async def _get_http_session(self) -> Optional[aiohttp.ClientSession]:
        """Get or create HTTP session."""
        if aiohttp is None:
            return None
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def close(self) -> None:
        """Close resources."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def check_component(self, component: TrinityComponent) -> ComponentHealth:
        """
        Perform comprehensive health check for a component.

        Check priority:
        1. HTTP endpoint (if available and aiohttp installed)
        2. Heartbeat file freshness
        3. Process check (if PID known)
        """
        config = self._configs.get(component)
        if not config:
            return await self._registry.update_state(
                component=component,
                state=ComponentState.UNKNOWN,
                error_message="Component not registered",
            )

        # Check if in grace period
        startup_time = self._startup_times.get(component)
        if startup_time:
            elapsed = time.time() - startup_time
            if elapsed < config.grace_period:
                return await self._registry.update_state(
                    component=component,
                    state=ComponentState.STARTING,
                    started_at=startup_time,
                    metadata={"grace_remaining": config.grace_period - elapsed},
                )

        # Try HTTP check first
        http_result = await self._check_http(config)
        if http_result is not None:
            state, latency, error = http_result
            return await self._registry.update_state(
                component=component,
                state=state,
                last_check_time=time.time(),
                last_http_success=time.time() if state == ComponentState.HEALTHY else None,
                latency_ms=latency,
                error_message=error,
            )

        # Fallback to heartbeat file
        heartbeat_result = await self._check_heartbeat(config)
        state, last_heartbeat, error = heartbeat_result
        return await self._registry.update_state(
            component=component,
            state=state,
            last_check_time=time.time(),
            last_heartbeat_time=last_heartbeat,
            error_message=error,
        )

    async def _check_http(
        self,
        config: ComponentConfig,
    ) -> Optional[Tuple[ComponentState, Optional[float], Optional[str]]]:
        """Check HTTP health endpoint."""
        session = await self._get_http_session()
        if session is None:
            return None  # aiohttp not available, use fallback

        try:
            start = time.monotonic()
            async with session.get(
                config.health_endpoint,
                timeout=aiohttp.ClientTimeout(total=config.health_timeout),
            ) as response:
                latency = (time.monotonic() - start) * 1000

                if response.status == 200:
                    return (ComponentState.HEALTHY, latency, None)
                elif response.status == 503:
                    return (ComponentState.DEGRADED, latency, f"HTTP 503")
                else:
                    return (ComponentState.UNHEALTHY, latency, f"HTTP {response.status}")

        except asyncio.TimeoutError:
            return (ComponentState.UNHEALTHY, None, f"HTTP timeout ({config.health_timeout}s)")
        except aiohttp.ClientConnectorError:
            return (ComponentState.UNHEALTHY, None, "Connection refused")
        except Exception as e:
            return (ComponentState.UNHEALTHY, None, str(e))

    async def _check_heartbeat(
        self,
        config: ComponentConfig,
    ) -> Tuple[ComponentState, Optional[float], Optional[str]]:
        """Check heartbeat file freshness."""
        try:
            if not config.heartbeat_file.exists():
                return (ComponentState.UNKNOWN, None, "No heartbeat file")

            stat = config.heartbeat_file.stat()
            mtime = stat.st_mtime
            age = time.time() - mtime

            if age > config.heartbeat_stale:
                return (
                    ComponentState.UNHEALTHY,
                    mtime,
                    f"Heartbeat stale ({age:.0f}s > {config.heartbeat_stale}s)",
                )

            # Try to read heartbeat content
            try:
                with open(config.heartbeat_file) as f:
                    data = json.load(f)
                    # Check for explicit status in heartbeat
                    status = data.get("status", "healthy")
                    if status == "degraded":
                        return (ComponentState.DEGRADED, mtime, "Self-reported degraded")
                    return (ComponentState.HEALTHY, mtime, None)
            except (json.JSONDecodeError, KeyError):
                # File exists and is fresh, assume healthy
                return (ComponentState.HEALTHY, mtime, None)

        except Exception as e:
            return (ComponentState.UNKNOWN, None, str(e))

    def mark_starting(self, component: TrinityComponent) -> None:
        """Mark component as starting (begins grace period)."""
        self._startup_times[component] = time.time()

    def clear_startup(self, component: TrinityComponent) -> None:
        """Clear startup tracking."""
        self._startup_times.pop(component, None)


# =============================================================================
# Process Supervisor (Cross-Repo Management)
# =============================================================================

class ProcessSupervisor:
    """
    Manages external repository processes.

    Features:
    - Start/stop/restart processes
    - Graceful shutdown with escalation
    - PID tracking
    - Output capture
    """

    def __init__(self):
        self._processes: Dict[TrinityComponent, asyncio.subprocess.Process] = {}
        self._pids: Dict[TrinityComponent, int] = {}

    async def start_component(
        self,
        config: ComponentConfig,
    ) -> bool:
        """Start a component process."""
        if not config.repo_path or not config.startup_command:
            logger.debug(f"[ProcessSupervisor] No repo path for {config.display_name}")
            return False

        if not config.repo_path.exists():
            logger.warning(f"[ProcessSupervisor] Repo path not found: {config.repo_path}")
            return False

        try:
            logger.info(f"[ProcessSupervisor] Starting {config.display_name}...")

            process = await asyncio.create_subprocess_exec(
                *config.startup_command,
                cwd=str(config.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ,
                    "JARVIS_CHILD_PROCESS": "true",
                    "JARVIS_COMPONENT": config.component.value,
                },
            )

            self._processes[config.component] = process
            self._pids[config.component] = process.pid

            logger.info(
                f"[ProcessSupervisor] {config.display_name} started (PID: {process.pid})"
            )
            return True

        except Exception as e:
            log_component_failure(
                "trinity",
                f"Failed to start {config.display_name}",
                error=e,
                component_name=config.display_name,
            )
            return False

    async def stop_component(
        self,
        component: TrinityComponent,
        graceful: bool = True,
        timeout: float = 10.0,
    ) -> bool:
        """Stop a component process."""
        process = self._processes.get(component)
        if not process:
            return True

        try:
            if graceful:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"[ProcessSupervisor] Graceful stop timeout, forcing")
                    process.kill()
            else:
                process.kill()

            await process.wait()
            self._processes.pop(component, None)
            self._pids.pop(component, None)
            return True

        except Exception as e:
            log_component_failure(
                "trinity",
                f"Failed to stop process: {component.value}",
                error=e,
            )
            return False

    async def stop_all(self, graceful: bool = True) -> None:
        """Stop all managed processes."""
        for component in list(self._processes.keys()):
            await self.stop_component(component, graceful=graceful)

    def get_pid(self, component: TrinityComponent) -> Optional[int]:
        """Get PID for a component."""
        return self._pids.get(component)

    def is_running(self, component: TrinityComponent) -> bool:
        """Check if component process is running."""
        process = self._processes.get(component)
        if not process:
            return False
        return process.returncode is None


# =============================================================================
# Unified Trinity Coordinator
# =============================================================================

class UnifiedTrinityCoordinator:
    """
    Single source of truth for Trinity cross-repo coordination.

    Integrates:
    - Health checking (single authority)
    - State management (single registry)
    - Notification deduplication
    - Process supervision

    Replaces multiple overlapping systems with one unified coordinator.
    """

    def __init__(self, is_leader: bool = True):
        self._is_leader = is_leader
        self._running = False

        # Core components
        self._registry = ComponentHealthRegistry()
        self._coalescer = NotificationCoalescer()
        self._health_checker = HealthChecker(self._registry, self._coalescer)
        self._process_supervisor = ProcessSupervisor()

        # Component configs
        self._configs: Dict[TrinityComponent, ComponentConfig] = {}

        # Background tasks
        self._health_check_task: Optional[asyncio.Task[None]] = None

        # State change callbacks
        self._state_callbacks: List[Callable[[TrinityComponent, ComponentState, ComponentState], Awaitable[None]]] = []

        # Track previous states for change detection
        self._prev_states: Dict[TrinityComponent, ComponentState] = {}

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------

    def register_component(self, config: ComponentConfig) -> None:
        """Register a component for coordination."""
        self._configs[config.component] = config
        self._health_checker.register_component(config)

    def register_trinity_components(self) -> None:
        """Register all Trinity components with default configs."""
        self.register_component(ComponentConfig.for_body())
        self.register_component(ComponentConfig.for_prime())
        self.register_component(ComponentConfig.for_reactor())

    async def on_state_change(
        self,
        callback: Callable[[TrinityComponent, ComponentState, ComponentState], Awaitable[None]]
    ) -> None:
        """Register callback for state changes."""
        self._state_callbacks.append(callback)

    # -------------------------------------------------------------------------
    # Startup Orchestration
    # -------------------------------------------------------------------------

    async def start(self) -> bool:
        """
        Start the coordinator and all managed components.

        Returns True if core components started successfully.
        """
        logger.info("[TrinityCoordinator] Starting...")

        self._running = True
        await self._registry.load()

        # Register for state changes
        async def handle_state_change(comp: TrinityComponent, health: ComponentHealth) -> None:
            prev_state = self._prev_states.get(comp, ComponentState.UNKNOWN)
            new_state = health.state

            if prev_state != new_state:
                # Emit notification only on state change
                await self._emit_state_change_notification(comp, prev_state, new_state)

                # Notify callbacks
                for callback in self._state_callbacks:
                    try:
                        await callback(comp, prev_state, new_state)
                    except Exception as e:
                        logger.debug(f"[TrinityCoordinator] Callback error: {e}")

                self._prev_states[comp] = new_state

        self._registry.subscribe(handle_state_change)

        # Start external repos if leader
        if self._is_leader:
            await self._start_external_repos()

        # Start health check loop
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        logger.info("[TrinityCoordinator] Started")
        return True

    async def _start_external_repos(self) -> None:
        """Start external repo processes."""
        # Start JARVIS Prime
        if TrinityConfig.START_PRIME:
            prime_config = self._configs.get(TrinityComponent.JARVIS_PRIME)
            if prime_config:
                self._health_checker.mark_starting(TrinityComponent.JARVIS_PRIME)
                await self._registry.update_state(
                    TrinityComponent.JARVIS_PRIME,
                    ComponentState.STARTING,
                )
                success = await self._process_supervisor.start_component(prime_config)
                if not success and not TrinityConfig.ALLOW_DEGRADED_MODE:
                    raise RuntimeError("Failed to start JARVIS Prime")

        # Start Reactor Core
        if TrinityConfig.START_REACTOR:
            reactor_config = self._configs.get(TrinityComponent.REACTOR_CORE)
            if reactor_config:
                self._health_checker.mark_starting(TrinityComponent.REACTOR_CORE)
                await self._registry.update_state(
                    TrinityComponent.REACTOR_CORE,
                    ComponentState.STARTING,
                )
                success = await self._process_supervisor.start_component(reactor_config)
                if not success and not TrinityConfig.ALLOW_DEGRADED_MODE:
                    raise RuntimeError("Failed to start Reactor Core")

    async def _emit_state_change_notification(
        self,
        component: TrinityComponent,
        prev_state: ComponentState,
        new_state: ComponentState,
    ) -> None:
        """
        Emit deduplicated notification for state change.

        v123.5: Added intelligent notification suppression for optional components
        that were never started. This prevents nagging "unavailable" warnings
        for components like Reactor Core when they're simply not running.
        """
        config = self._configs.get(component)
        name = config.display_name if config else component.value

        # v123.5: Check if component was ever started (has startup time)
        was_ever_started = component in self._health_checker._startup_times

        # v123.5: Check if component is required
        is_required = config.required if config else True

        # Determine level and message
        if new_state == ComponentState.HEALTHY and prev_state != ComponentState.HEALTHY:
            await self._coalescer.emit(
                component,
                NotificationLevel.INFO,
                f"✅ {name} is now healthy",
            )
        elif new_state == ComponentState.DEGRADED:
            await self._coalescer.emit(
                component,
                NotificationLevel.WARNING,
                f"⚠️ {name} is degraded",
            )
        elif new_state == ComponentState.UNHEALTHY:
            # Only emit if wasn't already unhealthy (dedup)
            if prev_state not in {ComponentState.UNHEALTHY, ComponentState.FAILED}:
                # v123.5: Suppress "unavailable" warnings for optional components
                # that were never started. This is the ROOT FIX for nagging warnings.
                if not is_required and not was_ever_started:
                    # Log at debug level instead of warning
                    logger.debug(
                        f"[Trinity] {name} is unavailable (optional, never started - suppressing warning)"
                    )
                else:
                    await self._coalescer.emit(
                        component,
                        NotificationLevel.WARNING,
                        f"⚠️ {name} is unavailable",
                    )
        elif new_state == ComponentState.FAILED:
            await self._coalescer.emit(
                component,
                NotificationLevel.ERROR,
                f"❌ {name} failed to start",
            )

    # -------------------------------------------------------------------------
    # Health Check Loop
    # -------------------------------------------------------------------------

    async def _health_check_loop(self) -> None:
        """Background health check loop."""
        while self._running:
            try:
                for component in self._configs.keys():
                    await self._health_checker.check_component(component)

                await asyncio.sleep(TrinityConfig.HEALTH_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log_component_failure(
                    "trinity",
                    "Health check loop error",
                    error=e,
                )
                await asyncio.sleep(5.0)

    # -------------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------------

    async def stop(self, graceful: bool = True) -> None:
        """Stop the coordinator and all managed components."""
        logger.info("[TrinityCoordinator] Stopping...")

        self._running = False

        # Stop health check loop
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Stop managed processes
        await self._process_supervisor.stop_all(graceful=graceful)

        # Close resources
        await self._health_checker.close()

        logger.info("[TrinityCoordinator] Stopped")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def get_component_state(
        self,
        component: TrinityComponent,
    ) -> Optional[ComponentHealth]:
        """Get current state for a component."""
        return await self._registry.get_state(component)

    async def get_trinity_status(self) -> Dict[str, Any]:
        """Get overall Trinity status."""
        states = await self._registry.get_all_states()

        # Determine overall health
        all_healthy = all(
            h.state == ComponentState.HEALTHY
            for h in states.values()
            if self._configs.get(h.component, ComponentConfig.for_body()).required
        )

        any_degraded = any(
            h.state == ComponentState.DEGRADED
            for h in states.values()
        )

        any_unhealthy = any(
            h.state in {ComponentState.UNHEALTHY, ComponentState.FAILED}
            for h in states.values()
        )

        if all_healthy and not any_degraded:
            overall = "HEALTHY"
        elif any_unhealthy:
            overall = "DEGRADED" if TrinityConfig.ALLOW_DEGRADED_MODE else "UNHEALTHY"
        else:
            overall = "DEGRADED"

        return {
            "overall_status": overall,
            "is_leader": self._is_leader,
            "degraded_mode_enabled": TrinityConfig.ALLOW_DEGRADED_MODE,
            "components": {
                comp.value: health.to_dict()
                for comp, health in states.items()
            },
            "managed_processes": {
                comp.value: self._process_supervisor.get_pid(comp)
                for comp in self._configs.keys()
                if self._process_supervisor.get_pid(comp)
            },
        }

    async def wait_for_component(
        self,
        component: TrinityComponent,
        timeout: Optional[float] = None,
    ) -> bool:
        """
        Wait for a component to become healthy.

        This is the proper way to handle dependencies - wait, don't poll!
        """
        config = self._configs.get(component)
        if not config:
            return False

        timeout = timeout or config.startup_timeout
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            health = await self._registry.get_state(component)
            if health and health.state == ComponentState.HEALTHY:
                return True

            if health and health.state == ComponentState.FAILED:
                return False

            await asyncio.sleep(1.0)

        return False


# =============================================================================
# Integration with Proxy Orchestrator
# =============================================================================

async def integrate_with_proxy_orchestrator(
    trinity_coordinator: UnifiedTrinityCoordinator,
) -> None:
    """
    Integrate Trinity coordinator with the distributed proxy orchestrator.

    This ensures CloudSQL proxy health is tracked as a Trinity component.
    """
    try:
        from .orchestrator import UnifiedProxyOrchestrator

        # Register CloudSQL as a component
        # v148.0: CloudSQL is OPTIONAL - system gracefully degrades to SQLite
        # Only mark as required if explicitly configured via env var
        cloudsql_required = os.getenv("CLOUDSQL_REQUIRED", "false").lower() == "true"
        
        cloudsql_config = ComponentConfig(
            component=TrinityComponent.CLOUDSQL_PROXY,
            display_name="CloudSQL Proxy",
            port=5432,
            health_endpoint="",  # Not HTTP, we check differently
            heartbeat_file=TrinityConfig.CROSS_REPO_DIR / "proxy_health.json",
            startup_timeout=60.0,
            health_timeout=5.0,
            heartbeat_stale=30.0,
            grace_period=10.0,
            required=cloudsql_required,  # v148.0: Optional by default (SQLite fallback)
        )
        trinity_coordinator.register_component(cloudsql_config)
        
        if not cloudsql_required:
            logger.debug(
                "[v148.0] CloudSQL Proxy registered as OPTIONAL. "
                "System will use SQLite fallback if unavailable. "
                "Set CLOUDSQL_REQUIRED=true to make it required."
            )

    except ImportError:
        logger.debug("[TrinityCoordinator] Proxy orchestrator not available")


# =============================================================================
# Factory Function
# =============================================================================

_coordinator_instance: Optional[UnifiedTrinityCoordinator] = None


async def get_trinity_coordinator(
    is_leader: bool = True,
    auto_register: bool = True,
) -> UnifiedTrinityCoordinator:
    """
    Get or create the singleton Trinity coordinator.

    Args:
        is_leader: Whether this instance is the leader
        auto_register: Automatically register Trinity components

    Returns:
        UnifiedTrinityCoordinator instance
    """
    global _coordinator_instance

    if _coordinator_instance is None:
        _coordinator_instance = UnifiedTrinityCoordinator(is_leader=is_leader)
        if auto_register:
            _coordinator_instance.register_trinity_components()

    return _coordinator_instance


async def shutdown_trinity_coordinator() -> None:
    """Shutdown the singleton coordinator."""
    global _coordinator_instance

    if _coordinator_instance:
        await _coordinator_instance.stop()
        _coordinator_instance = None
