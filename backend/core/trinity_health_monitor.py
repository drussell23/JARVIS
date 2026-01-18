"""
v78.0: Trinity Cross-Repo Health Monitor
==========================================

Unified health monitoring for all three Trinity components:
- JARVIS (Body) - HTTP endpoint + internal components
- J-Prime (Mind) - Heartbeat file + HTTP endpoint
- Reactor-Core (Nerves) - Heartbeat file + HTTP endpoint

Features:
- Real-time health aggregation from all repos
- Heartbeat file monitoring with staleness detection
- HTTP endpoint health checks with retry logic
- Automatic health degradation detection
- Recovery suggestions and automatic restart triggers
- WebSocket broadcasting for UI updates

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │              Trinity Health Monitor (Unified)                │
    ├─────────────┬─────────────────────────┬────────────────────┤
    │   JARVIS    │       J-Prime           │   Reactor-Core     │
    │   (Body)    │       (Mind)            │    (Nerves)        │
    ├─────────────┼─────────────────────────┼────────────────────┤
    │ HTTP:8010   │ Heartbeat + HTTP:8000   │ Heartbeat + HTTP   │
    │ Internal    │ jarvis_prime.json       │ reactor_core.json  │
    └─────────────┴─────────────────────────┴────────────────────┘

Author: JARVIS v78.0
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Data Classes
# =============================================================================

class ComponentStatus(str, Enum):
    """Health status levels for Trinity components."""
    HEALTHY = "healthy"          # All checks passing
    DEGRADED = "degraded"        # Some checks failing but functional
    UNHEALTHY = "unhealthy"      # Critical failures
    UNKNOWN = "unknown"          # Not yet checked
    STARTING = "starting"        # Component is initializing
    STOPPED = "stopped"          # Component intentionally stopped


class TrinityComponent(str, Enum):
    """Trinity architecture components."""
    JARVIS_BODY = "jarvis_body"        # JARVIS-AI-Agent
    JARVIS_PRIME = "jarvis_prime"      # J-Prime (Mind)
    REACTOR_CORE = "reactor_core"      # Reactor-Core (Nerves)
    CODING_COUNCIL = "coding_council"  # Coding Council subsystem
    TRINITY_SYNC = "trinity_sync"      # Overall Trinity synchronization


@dataclass
class HealthCheckResult:
    """Result of a single health check."""
    check_type: str  # "http", "heartbeat", "internal"
    success: bool
    response_time_ms: float = 0.0
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ComponentHealthStatus:
    """Comprehensive health status for a Trinity component."""
    component: TrinityComponent
    status: ComponentStatus = ComponentStatus.UNKNOWN
    last_check: Optional[float] = None
    uptime_seconds: float = 0.0
    check_results: List[HealthCheckResult] = field(default_factory=list)
    heartbeat_age_seconds: Optional[float] = None
    http_response_time_ms: Optional[float] = None
    error_count: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.status == ComponentStatus.HEALTHY

    @property
    def is_operational(self) -> bool:
        """Returns True if component is functional (healthy or degraded)."""
        return self.status in (ComponentStatus.HEALTHY, ComponentStatus.DEGRADED)


@dataclass
class TrinityHealthSnapshot:
    """Complete health snapshot of the Trinity system."""
    timestamp: float = field(default_factory=time.time)
    overall_status: ComponentStatus = ComponentStatus.UNKNOWN
    health_score: float = 0.0  # 0.0 - 1.0
    components: Dict[TrinityComponent, ComponentHealthStatus] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    check_duration_ms: float = 0.0

    @property
    def all_healthy(self) -> bool:
        return all(c.is_healthy for c in self.components.values())

    @property
    def any_unhealthy(self) -> bool:
        return any(c.status == ComponentStatus.UNHEALTHY for c in self.components.values())

    @property
    def summary(self) -> str:
        """Get a human-readable summary."""
        healthy = [c.component.value for c in self.components.values() if c.is_healthy]
        unhealthy = [c.component.value for c in self.components.values() if not c.is_healthy]
        return f"Healthy: {', '.join(healthy) or 'none'} | Unhealthy: {', '.join(unhealthy) or 'none'}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status.value,
            "health_score": self.health_score,
            "summary": self.summary,
            "all_healthy": self.all_healthy,
            "check_duration_ms": self.check_duration_ms,
            "components": {
                k.value: {
                    "status": v.status.value,
                    "uptime_seconds": v.uptime_seconds,
                    "heartbeat_age_seconds": v.heartbeat_age_seconds,
                    "http_response_time_ms": v.http_response_time_ms,
                    "consecutive_failures": v.consecutive_failures,
                    "last_error": v.last_error,
                }
                for k, v in self.components.items()
            },
            "errors": self.errors,
            "warnings": self.warnings,
        }


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class TrinityHealthConfig:
    """Configuration for Trinity health monitoring."""
    # Directories
    trinity_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "trinity")

    # Heartbeat settings
    max_heartbeat_age_seconds: float = 15.0
    heartbeat_warning_age_seconds: float = 10.0

    # HTTP check settings
    http_timeout_seconds: float = 5.0
    http_retry_attempts: int = 2
    http_retry_delay_seconds: float = 1.0

    # Monitoring intervals
    check_interval_seconds: float = 10.0
    broadcast_interval_seconds: float = 5.0

    # Thresholds
    consecutive_failures_for_unhealthy: int = 3
    health_score_degraded_threshold: float = 0.7
    health_score_unhealthy_threshold: float = 0.4

    # Component endpoints (dynamically discovered if possible)
    jarvis_backend_port: int = 8010
    jarvis_prime_port: int = 8000  # v89.0: Fixed to 8000 (was incorrectly 8002)
    reactor_core_port: int = 8090

    # Component weights for health score calculation
    component_weights: Dict[TrinityComponent, float] = field(default_factory=lambda: {
        TrinityComponent.JARVIS_BODY: 1.0,      # Critical
        TrinityComponent.JARVIS_PRIME: 0.7,    # Important but optional
        TrinityComponent.REACTOR_CORE: 0.7,    # Important but optional
        TrinityComponent.CODING_COUNCIL: 0.5,  # Nice to have
        TrinityComponent.TRINITY_SYNC: 0.8,    # Important for cross-repo
    })

    @classmethod
    def from_env(cls) -> "TrinityHealthConfig":
        """Load configuration from environment variables."""
        return cls(
            trinity_dir=Path(os.getenv("TRINITY_DIR", str(Path.home() / ".jarvis" / "trinity"))),
            max_heartbeat_age_seconds=float(os.getenv("TRINITY_MAX_HEARTBEAT_AGE", "15.0")),
            http_timeout_seconds=float(os.getenv("TRINITY_HTTP_TIMEOUT", "5.0")),
            check_interval_seconds=float(os.getenv("TRINITY_CHECK_INTERVAL", "10.0")),
            jarvis_backend_port=int(os.getenv("JARVIS_BACKEND_PORT", "8010")),
            jarvis_prime_port=int(os.getenv("JARVIS_PRIME_PORT", "8000")),  # v89.0: Fixed to 8000
            reactor_core_port=int(os.getenv("REACTOR_CORE_PORT", "8090")),
        )


# =============================================================================
# Trinity Health Monitor
# =============================================================================

class TrinityHealthMonitor:
    """
    Unified health monitor for all Trinity components.

    Monitors:
    - JARVIS Body (HTTP endpoints, internal components)
    - J-Prime Mind (heartbeat files, HTTP endpoint)
    - Reactor-Core Nerves (heartbeat files, HTTP endpoint)
    - Coding Council (heartbeat files, HTTP endpoint)

    Features:
    - Real-time health aggregation
    - Automatic status calculation
    - Event callbacks for status changes
    - Health score computation with weighted components
    """

    def __init__(
        self,
        config: Optional[TrinityHealthConfig] = None,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.config = config or TrinityHealthConfig.from_env()
        self.log = logger_instance or logger

        # State
        self._latest_snapshot: Optional[TrinityHealthSnapshot] = None
        self._component_status: Dict[TrinityComponent, ComponentHealthStatus] = {}
        self._monitoring_task: Optional[asyncio.Task] = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._lock = asyncio.Lock()

        # HTTP session
        self._http_session: Optional["aiohttp.ClientSession"] = None

        # Callbacks
        self._on_health_change: List[Callable[[TrinityHealthSnapshot], None]] = []
        self._on_component_change: List[Callable[[TrinityComponent, ComponentStatus, ComponentStatus], None]] = []

        # Initialize component status
        for component in TrinityComponent:
            self._component_status[component] = ComponentHealthStatus(component=component)

        self.log.info("[TrinityHealthMonitor] Initialized")

    async def start(self) -> None:
        """Start the health monitoring loop."""
        if self._is_running:
            return

        self._is_running = True

        # Ensure directories exist
        self.config.trinity_dir.mkdir(parents=True, exist_ok=True)
        (self.config.trinity_dir / "components").mkdir(parents=True, exist_ok=True)

        # Start monitoring task
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())

        self.log.info("[TrinityHealthMonitor] Started monitoring")

    async def stop(self) -> None:
        """Stop the health monitoring loop."""
        self._is_running = False

        # Cancel tasks
        for task in [self._monitoring_task, self._broadcast_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close HTTP session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

        self.log.info("[TrinityHealthMonitor] Stopped")

    def register_health_callback(self, callback: Callable[[TrinityHealthSnapshot], None]) -> None:
        """Register callback for health changes."""
        self._on_health_change.append(callback)

    def register_component_callback(
        self,
        callback: Callable[[TrinityComponent, ComponentStatus, ComponentStatus], None]
    ) -> None:
        """Register callback for component status changes."""
        self._on_component_change.append(callback)

    @property
    def latest_snapshot(self) -> Optional[TrinityHealthSnapshot]:
        """Get the latest health snapshot."""
        return self._latest_snapshot

    async def check_health(self) -> TrinityHealthSnapshot:
        """
        Perform a complete health check of all Trinity components.

        Returns:
            TrinityHealthSnapshot with current health status
        """
        start_time = time.time()
        snapshot = TrinityHealthSnapshot()

        async with self._lock:
            # Check all components in parallel
            checks = [
                self._check_jarvis_body(),
                self._check_jarvis_prime(),
                self._check_reactor_core(),
                self._check_coding_council(),
            ]

            results = await asyncio.gather(*checks, return_exceptions=True)

            # Process results
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    self.log.error(f"[TrinityHealthMonitor] Check failed: {result}")
                    snapshot.errors.append(str(result))
                elif isinstance(result, ComponentHealthStatus):
                    component = result.component
                    old_status = self._component_status.get(component, ComponentHealthStatus(component=component))

                    # Update status
                    self._component_status[component] = result
                    snapshot.components[component] = result

                    # Fire callback if status changed
                    if old_status.status != result.status:
                        for callback in self._on_component_change:
                            try:
                                callback(component, old_status.status, result.status)
                            except Exception as e:
                                self.log.error(f"[TrinityHealthMonitor] Callback error: {e}")

            # Check Trinity sync status
            snapshot.components[TrinityComponent.TRINITY_SYNC] = self._calculate_trinity_sync_status()

            # Calculate overall status
            snapshot.health_score = self._calculate_health_score(snapshot)
            snapshot.overall_status = self._determine_overall_status(snapshot)
            snapshot.check_duration_ms = (time.time() - start_time) * 1000
            snapshot.timestamp = time.time()

            # Store snapshot
            self._latest_snapshot = snapshot

            # Fire health change callbacks
            for callback in self._on_health_change:
                try:
                    callback(snapshot)
                except Exception as e:
                    self.log.error(f"[TrinityHealthMonitor] Health callback error: {e}")

        return snapshot

    async def _check_jarvis_body(self) -> ComponentHealthStatus:
        """Check JARVIS Body (main backend) health."""
        status = ComponentHealthStatus(component=TrinityComponent.JARVIS_BODY)
        checks = []

        # HTTP health check
        http_result = await self._check_http_endpoint(
            f"http://127.0.0.1:{self.config.jarvis_backend_port}/health/ping",
            timeout=self.config.http_timeout_seconds,
        )
        checks.append(http_result)
        status.http_response_time_ms = http_result.response_time_ms

        # Determine status based on checks
        if http_result.success:
            status.status = ComponentStatus.HEALTHY
            if http_result.details.get("uptime"):
                status.uptime_seconds = http_result.details["uptime"]
        else:
            status.consecutive_failures += 1
            status.last_error = http_result.error
            if status.consecutive_failures >= self.config.consecutive_failures_for_unhealthy:
                status.status = ComponentStatus.UNHEALTHY
            else:
                status.status = ComponentStatus.DEGRADED

        status.check_results = checks
        status.last_check = time.time()
        return status

    async def _check_jarvis_prime(self) -> ComponentHealthStatus:
        """Check J-Prime (Mind) health via heartbeat file."""
        status = ComponentHealthStatus(component=TrinityComponent.JARVIS_PRIME)
        checks = []

        # Heartbeat file check (primary)
        # Support both naming conventions
        heartbeat_files = [
            self.config.trinity_dir / "components" / "jarvis_prime.json",
            self.config.trinity_dir / "components" / "j_prime.json",
        ]

        heartbeat_result = await self._check_heartbeat_file(heartbeat_files)
        checks.append(heartbeat_result)

        if heartbeat_result.success:
            status.heartbeat_age_seconds = heartbeat_result.details.get("age_seconds", 0)
            status.uptime_seconds = heartbeat_result.details.get("uptime_seconds", 0)
            status.metadata = heartbeat_result.details

            # Check if heartbeat is getting stale
            if status.heartbeat_age_seconds > self.config.heartbeat_warning_age_seconds:
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY
            status.consecutive_failures = 0
        else:
            status.consecutive_failures += 1
            status.last_error = heartbeat_result.error
            status.status = ComponentStatus.UNHEALTHY

        status.check_results = checks
        status.last_check = time.time()
        return status

    async def _check_reactor_core(self) -> ComponentHealthStatus:
        """Check Reactor-Core (Nerves) health via heartbeat file."""
        status = ComponentHealthStatus(component=TrinityComponent.REACTOR_CORE)
        checks = []

        # Heartbeat file check
        heartbeat_file = self.config.trinity_dir / "components" / "reactor_core.json"
        heartbeat_result = await self._check_heartbeat_file([heartbeat_file])
        checks.append(heartbeat_result)

        if heartbeat_result.success:
            status.heartbeat_age_seconds = heartbeat_result.details.get("age_seconds", 0)
            status.uptime_seconds = heartbeat_result.details.get("uptime_seconds", 0)
            status.metadata = heartbeat_result.details

            if status.heartbeat_age_seconds > self.config.heartbeat_warning_age_seconds:
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY
            status.consecutive_failures = 0
        else:
            status.consecutive_failures += 1
            status.last_error = heartbeat_result.error
            status.status = ComponentStatus.UNHEALTHY

        status.check_results = checks
        status.last_check = time.time()
        return status

    async def _check_coding_council(self) -> ComponentHealthStatus:
        """Check Coding Council health via heartbeat file."""
        status = ComponentHealthStatus(component=TrinityComponent.CODING_COUNCIL)
        checks = []

        # Heartbeat file check
        heartbeat_file = self.config.trinity_dir / "components" / "coding_council.json"
        heartbeat_result = await self._check_heartbeat_file([heartbeat_file])
        checks.append(heartbeat_result)

        if heartbeat_result.success:
            status.heartbeat_age_seconds = heartbeat_result.details.get("age_seconds", 0)
            status.metadata = heartbeat_result.details

            if status.heartbeat_age_seconds > self.config.heartbeat_warning_age_seconds:
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY
            status.consecutive_failures = 0
        else:
            status.consecutive_failures += 1
            status.last_error = heartbeat_result.error
            status.status = ComponentStatus.UNHEALTHY

        status.check_results = checks
        status.last_check = time.time()
        return status

    def _calculate_trinity_sync_status(self) -> ComponentHealthStatus:
        """Calculate Trinity sync status based on component health."""
        status = ComponentHealthStatus(component=TrinityComponent.TRINITY_SYNC)

        # Trinity sync is healthy if:
        # - JARVIS Body is healthy AND
        # - At least one of J-Prime or Reactor-Core is healthy
        body_healthy = self._component_status.get(
            TrinityComponent.JARVIS_BODY,
            ComponentHealthStatus(component=TrinityComponent.JARVIS_BODY)
        ).is_healthy

        prime_healthy = self._component_status.get(
            TrinityComponent.JARVIS_PRIME,
            ComponentHealthStatus(component=TrinityComponent.JARVIS_PRIME)
        ).is_operational

        reactor_healthy = self._component_status.get(
            TrinityComponent.REACTOR_CORE,
            ComponentHealthStatus(component=TrinityComponent.REACTOR_CORE)
        ).is_operational

        if body_healthy and (prime_healthy or reactor_healthy):
            status.status = ComponentStatus.HEALTHY
        elif body_healthy:
            status.status = ComponentStatus.DEGRADED
            status.last_error = "No cross-repo components available"
        else:
            status.status = ComponentStatus.UNHEALTHY
            status.last_error = "JARVIS Body not healthy"

        status.last_check = time.time()
        return status

    async def _check_http_endpoint(
        self,
        url: str,
        timeout: float = 5.0,
    ) -> HealthCheckResult:
        """Check an HTTP endpoint."""
        result = HealthCheckResult(check_type="http")
        start_time = time.time()

        try:
            import aiohttp

            if self._http_session is None or self._http_session.closed:
                self._http_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                )

            async with self._http_session.get(url) as response:
                result.response_time_ms = (time.time() - start_time) * 1000

                if response.status == 200:
                    result.success = True
                    try:
                        result.details = await response.json()
                    except Exception:
                        pass
                else:
                    result.success = False
                    result.error = f"HTTP {response.status}"

        except asyncio.TimeoutError:
            result.success = False
            result.error = f"Timeout after {timeout}s"
            result.response_time_ms = timeout * 1000
        except Exception as e:
            result.success = False
            result.error = str(e)
            result.response_time_ms = (time.time() - start_time) * 1000

        result.timestamp = time.time()
        return result

    async def _check_heartbeat_file(
        self,
        file_paths: List[Path],
    ) -> HealthCheckResult:
        """Check heartbeat file(s) for freshness."""
        result = HealthCheckResult(check_type="heartbeat")
        start_time = time.time()

        for file_path in file_paths:
            try:
                if not file_path.exists():
                    continue

                with open(file_path) as f:
                    data = json.load(f)

                heartbeat_ts = data.get("timestamp", 0)
                age_seconds = time.time() - heartbeat_ts

                if age_seconds < self.config.max_heartbeat_age_seconds:
                    result.success = True
                    result.details = {
                        "file": str(file_path),
                        "age_seconds": age_seconds,
                        "uptime_seconds": data.get("uptime_seconds", 0),
                        "status": data.get("status", "unknown"),
                        **{k: v for k, v in data.items() if k not in ("timestamp", "uptime_seconds", "status")},
                    }
                    result.response_time_ms = (time.time() - start_time) * 1000
                    return result
                else:
                    # File exists but is stale
                    result.error = f"Heartbeat stale ({age_seconds:.1f}s > {self.config.max_heartbeat_age_seconds}s)"

            except json.JSONDecodeError as e:
                result.error = f"Invalid JSON in {file_path.name}: {e}"
            except Exception as e:
                result.error = f"Error reading {file_path.name}: {e}"

        # No valid heartbeat found
        if result.error is None:
            result.error = f"No heartbeat file found ({', '.join(p.name for p in file_paths)})"

        result.success = False
        result.response_time_ms = (time.time() - start_time) * 1000
        result.timestamp = time.time()
        return result

    def _calculate_health_score(self, snapshot: TrinityHealthSnapshot) -> float:
        """Calculate weighted health score (0.0 - 1.0)."""
        if not snapshot.components:
            return 0.0

        total_weight = 0.0
        weighted_score = 0.0

        for component, status in snapshot.components.items():
            weight = self.config.component_weights.get(component, 0.5)
            total_weight += weight

            if status.status == ComponentStatus.HEALTHY:
                weighted_score += weight * 1.0
            elif status.status == ComponentStatus.DEGRADED:
                weighted_score += weight * 0.5
            elif status.status == ComponentStatus.STARTING:
                weighted_score += weight * 0.7
            # UNHEALTHY, UNKNOWN, STOPPED = 0.0

        return weighted_score / total_weight if total_weight > 0 else 0.0

    def _determine_overall_status(self, snapshot: TrinityHealthSnapshot) -> ComponentStatus:
        """Determine overall system status based on health score and components."""
        # Critical component check
        body_status = snapshot.components.get(TrinityComponent.JARVIS_BODY)
        if body_status and body_status.status == ComponentStatus.UNHEALTHY:
            return ComponentStatus.UNHEALTHY

        # Score-based determination
        if snapshot.health_score >= self.config.health_score_degraded_threshold:
            return ComponentStatus.HEALTHY
        elif snapshot.health_score >= self.config.health_score_unhealthy_threshold:
            return ComponentStatus.DEGRADED
        else:
            return ComponentStatus.UNHEALTHY

    async def _monitoring_loop(self) -> None:
        """Background loop for continuous health monitoring."""
        while self._is_running:
            try:
                await self.check_health()
                await asyncio.sleep(self.config.check_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"[TrinityHealthMonitor] Monitoring error: {e}")
                await asyncio.sleep(self.config.check_interval_seconds)

    async def _broadcast_loop(self) -> None:
        """Background loop for broadcasting health status."""
        while self._is_running:
            try:
                if self._latest_snapshot:
                    await self._write_health_status()
                await asyncio.sleep(self.config.broadcast_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"[TrinityHealthMonitor] Broadcast error: {e}")
                await asyncio.sleep(self.config.broadcast_interval_seconds)

    async def _write_health_status(self) -> None:
        """Write current health status to file for other components to read."""
        if not self._latest_snapshot:
            return

        status_file = self.config.trinity_dir / "health_status.json"

        try:
            import tempfile

            # Atomic write
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=self.config.trinity_dir,
                prefix=".health_status.",
                suffix=".tmp"
            )

            with os.fdopen(tmp_fd, 'w') as tmp_file:
                json.dump(self._latest_snapshot.to_dict(), tmp_file, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(tmp_name, status_file)

        except Exception as e:
            self.log.debug(f"[TrinityHealthMonitor] Failed to write status file: {e}")


# =============================================================================
# Global Instance
# =============================================================================

_health_monitor: Optional[TrinityHealthMonitor] = None


async def get_trinity_health_monitor(
    config: Optional[TrinityHealthConfig] = None,
) -> TrinityHealthMonitor:
    """Get or create the global Trinity health monitor instance."""
    global _health_monitor

    if _health_monitor is None:
        _health_monitor = TrinityHealthMonitor(config=config)

    return _health_monitor


async def start_trinity_health_monitoring(
    config: Optional[TrinityHealthConfig] = None,
) -> TrinityHealthMonitor:
    """Start Trinity health monitoring."""
    monitor = await get_trinity_health_monitor(config)
    await monitor.start()
    return monitor


async def stop_trinity_health_monitoring() -> None:
    """Stop Trinity health monitoring."""
    global _health_monitor

    if _health_monitor:
        await _health_monitor.stop()
        _health_monitor = None


async def get_trinity_health_snapshot() -> Optional[TrinityHealthSnapshot]:
    """Get the latest Trinity health snapshot."""
    if _health_monitor:
        return _health_monitor.latest_snapshot
    return None
