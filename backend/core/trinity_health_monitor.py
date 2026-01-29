"""
v108.0: Trinity Cross-Repo Health Monitor
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
- v108.0: Startup-aware health checking (uses TrinityOrchestrationConfig)
- v108.0: Component-specific timeout profiles
- v108.0: Grace period awareness during startup

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

CRITICAL v108.0 FIX: All timeouts now come from TrinityOrchestrationConfig
to prevent mismatched configuration values causing cascading failures.

Author: JARVIS v108.0
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
    # v125.0: New statuses for optional cross-repo components
    UNAVAILABLE = "unavailable"  # Component not configured/installed (not an error)
    OPTIONAL_OFFLINE = "optional_offline"  # Optional component offline (graceful degradation)


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
    check_type: str  # "http", "heartbeat", "internal", "readiness"
    success: bool = False  # v94.0: Default to False for safety
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

    @property
    def is_optional_offline(self) -> bool:
        """v125.0: Returns True if component is optional and offline (not an error)."""
        return self.status in (ComponentStatus.UNAVAILABLE, ComponentStatus.OPTIONAL_OFFLINE)

    @property
    def is_blocking_error(self) -> bool:
        """v125.0: Returns True if component status represents a blocking error."""
        # UNAVAILABLE and OPTIONAL_OFFLINE are NOT blocking errors
        return self.status == ComponentStatus.UNHEALTHY


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
    """
    Configuration for Trinity health monitoring.

    v108.0: Now integrates with TrinityOrchestrationConfig for unified configuration.
    Component-specific timeouts and startup grace periods are pulled from the
    orchestration config to ensure consistency across the system.
    """
    # Directories
    trinity_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "trinity")

    # v108.0: Heartbeat settings now pulled from orchestration config per-component
    # These are FALLBACK values only - actual values come from TrinityOrchestrationConfig
    max_heartbeat_age_seconds: float = 45.0  # Increased from 15.0 for J-Prime compatibility
    heartbeat_warning_age_seconds: float = 30.0  # Increased from 10.0

    # HTTP check settings - component-specific timeouts from orchestration config
    http_timeout_seconds: float = 10.0  # Increased from 5.0 for reliability
    http_retry_attempts: int = 3  # Increased from 2
    http_retry_delay_seconds: float = 2.0

    # Monitoring intervals
    check_interval_seconds: float = 10.0
    broadcast_interval_seconds: float = 5.0

    # Thresholds
    consecutive_failures_for_unhealthy: int = 3
    health_score_degraded_threshold: float = 0.7
    health_score_unhealthy_threshold: float = 0.4

    # Component endpoints (dynamically discovered if possible)
    jarvis_backend_port: int = 8010
    jarvis_prime_port: int = 8000
    reactor_core_port: int = 8090

    # Component weights for health score calculation
    component_weights: Dict[TrinityComponent, float] = field(default_factory=lambda: {
        TrinityComponent.JARVIS_BODY: 1.0,      # Critical
        TrinityComponent.JARVIS_PRIME: 0.7,    # Important but optional
        TrinityComponent.REACTOR_CORE: 0.7,    # Important but optional
        TrinityComponent.CODING_COUNCIL: 0.5,  # Nice to have
        TrinityComponent.TRINITY_SYNC: 0.8,    # Important for cross-repo
    })

    # v108.0: Track component startup times for grace period awareness
    _component_startup_times: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "TrinityHealthConfig":
        """Load configuration from environment variables."""
        return cls(
            trinity_dir=Path(os.getenv("TRINITY_DIR", str(Path.home() / ".jarvis" / "trinity"))),
            max_heartbeat_age_seconds=float(os.getenv("TRINITY_MAX_HEARTBEAT_AGE", "45.0")),
            http_timeout_seconds=float(os.getenv("TRINITY_HTTP_TIMEOUT", "10.0")),
            check_interval_seconds=float(os.getenv("TRINITY_CHECK_INTERVAL", "10.0")),
            jarvis_backend_port=int(os.getenv("JARVIS_BACKEND_PORT", "8010")),
            jarvis_prime_port=int(os.getenv("JARVIS_PRIME_PORT", "8000")),
            reactor_core_port=int(os.getenv("REACTOR_CORE_PORT", "8090")),
        )

    @classmethod
    def from_orchestration_config(cls) -> "TrinityHealthConfig":
        """
        v108.0: Create config from unified TrinityOrchestrationConfig.

        This ensures all timeout values are consistent across the system.
        """
        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
                ComponentType,
            )
            orch_config = get_orchestration_config()

            # Use the most lenient heartbeat threshold for general checks
            # Component-specific checks will use get_heartbeat_threshold()
            body_profile = orch_config.get_profile(ComponentType.JARVIS_BODY)
            prime_profile = orch_config.get_profile(ComponentType.JARVIS_PRIME)

            return cls(
                trinity_dir=orch_config.trinity_dir,
                max_heartbeat_age_seconds=max(
                    body_profile.heartbeat_stale,
                    prime_profile.heartbeat_stale,
                ),
                heartbeat_warning_age_seconds=body_profile.heartbeat_stale * 0.75,
                http_timeout_seconds=max(
                    body_profile.health_check_timeout,
                    prime_profile.health_check_timeout,
                ),
                http_retry_attempts=body_profile.retry_attempts,
                http_retry_delay_seconds=body_profile.retry_delay,
                check_interval_seconds=orch_config.health_check_interval,
                jarvis_backend_port=orch_config.jarvis_body_port,
                jarvis_prime_port=orch_config.jarvis_prime_port,
                reactor_core_port=orch_config.reactor_core_port,
            )
        except ImportError:
            logger.warning(
                "[TrinityHealthConfig] TrinityOrchestrationConfig not available, "
                "using environment defaults"
            )
            return cls.from_env()

    def get_heartbeat_threshold(self, component: TrinityComponent) -> float:
        """
        v108.0: Get component-specific heartbeat threshold.

        Critical fix: J-Prime needs longer thresholds due to ML model loading.
        """
        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
                ComponentType,
            )
            orch_config = get_orchestration_config()

            component_map = {
                TrinityComponent.JARVIS_BODY: ComponentType.JARVIS_BODY,
                TrinityComponent.JARVIS_PRIME: ComponentType.JARVIS_PRIME,
                TrinityComponent.REACTOR_CORE: ComponentType.REACTOR_CORE,
                TrinityComponent.CODING_COUNCIL: ComponentType.CODING_COUNCIL,
            }

            if component in component_map:
                profile = orch_config.get_profile(component_map[component])
                return profile.heartbeat_stale

        except ImportError:
            pass

        return self.max_heartbeat_age_seconds

    def get_http_timeout(self, component: TrinityComponent) -> float:
        """
        v108.0: Get component-specific HTTP timeout.
        """
        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
                ComponentType,
            )
            orch_config = get_orchestration_config()

            component_map = {
                TrinityComponent.JARVIS_BODY: ComponentType.JARVIS_BODY,
                TrinityComponent.JARVIS_PRIME: ComponentType.JARVIS_PRIME,
                TrinityComponent.REACTOR_CORE: ComponentType.REACTOR_CORE,
                TrinityComponent.CODING_COUNCIL: ComponentType.CODING_COUNCIL,
            }

            if component in component_map:
                profile = orch_config.get_profile(component_map[component])
                return profile.health_check_timeout

        except ImportError:
            pass

        return self.http_timeout_seconds

    def is_in_startup_grace_period(self, component: TrinityComponent) -> bool:
        """
        v108.0: Check if a component is in its startup grace period.

        During startup, components should NOT be marked as unhealthy
        even if heartbeats are missing or health checks fail.

        Returns:
            True if component is still in grace period
        """
        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
                ComponentType,
            )
            orch_config = get_orchestration_config()

            component_map = {
                TrinityComponent.JARVIS_BODY: ComponentType.JARVIS_BODY,
                TrinityComponent.JARVIS_PRIME: ComponentType.JARVIS_PRIME,
                TrinityComponent.REACTOR_CORE: ComponentType.REACTOR_CORE,
                TrinityComponent.CODING_COUNCIL: ComponentType.CODING_COUNCIL,
            }

            if component not in component_map:
                return False

            component_key = component.value
            startup_time = self._component_startup_times.get(component_key)

            if startup_time is None:
                # No recorded startup time - could be first check
                return False

            return orch_config.is_in_startup_grace_period(
                component_type=component_map[component],
                startup_time=startup_time,
            )

        except ImportError:
            return False

    def record_component_startup(self, component: TrinityComponent) -> None:
        """
        v108.0: Record when a component starts for grace period tracking.
        """
        self._component_startup_times[component.value] = time.time()
        logger.debug(f"[TrinityHealthConfig] Recorded startup time for {component.value}")


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

            # v117.5: Persist health state immediately for cross-repo sync
            # (in addition to periodic writes from _broadcast_loop)
            await self._write_health_status()

            # Fire health change callbacks
            for callback in self._on_health_change:
                try:
                    callback(snapshot)
                except Exception as e:
                    self.log.error(f"[TrinityHealthMonitor] Health callback error: {e}")

        return snapshot

    async def _check_jarvis_body(self) -> ComponentHealthStatus:
        """
        Check JARVIS Body (main backend) health.

        v94.0: Now uses /health/ready endpoint instead of /health/ping
        to properly detect initialization state. This fixes the false
        positive issue where health checks passed during initialization.
        """
        status = ComponentHealthStatus(component=TrinityComponent.JARVIS_BODY)
        checks = []

        # v94.0: First check readiness endpoint (the KEY fix for false positives)
        ready_result = await self._check_readiness_endpoint(
            f"http://127.0.0.1:{self.config.jarvis_backend_port}/health/ready",
            timeout=self.config.http_timeout_seconds,
        )
        checks.append(ready_result)
        status.http_response_time_ms = ready_result.response_time_ms

        # Determine status based on readiness check
        if ready_result.success:
            # Ready endpoint returned 200 - component is fully initialized
            ready_data = ready_result.details
            phase = ready_data.get("phase", "unknown")

            if phase == "healthy":
                status.status = ComponentStatus.HEALTHY
            elif phase in ("ready", "degraded"):
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY

            status.uptime_seconds = ready_data.get("uptime_seconds", 0)
            status.consecutive_failures = 0
            status.metadata["readiness_phase"] = phase
            status.metadata["ready_components"] = ready_data.get("ready_components", 0)
            status.metadata["total_components"] = ready_data.get("total_components", 0)
        else:
            # Ready endpoint returned non-200 (503 or error)
            # Check if it's still starting or actually unhealthy
            ready_data = ready_result.details

            if ready_result.error and "503" in str(ready_result.error):
                # 503 means "not ready yet" - component is STARTING
                phase = ready_data.get("phase", "starting")
                progress = ready_data.get("progress_percent", 0)

                status.status = ComponentStatus.STARTING
                status.metadata["readiness_phase"] = phase
                status.metadata["progress_percent"] = progress
                status.last_error = f"Still initializing ({progress:.0f}%)"

                self.log.debug(
                    f"[TrinityHealthMonitor] JARVIS Body in STARTING state "
                    f"(phase={phase}, progress={progress:.0f}%)"
                )
            else:
                # Actual failure (connection refused, timeout, etc.)
                status.consecutive_failures += 1
                status.last_error = ready_result.error

                if status.consecutive_failures >= self.config.consecutive_failures_for_unhealthy:
                    status.status = ComponentStatus.UNHEALTHY
                else:
                    status.status = ComponentStatus.DEGRADED

        status.check_results = checks
        status.last_check = time.time()
        return status

    async def _check_jarvis_prime(self) -> ComponentHealthStatus:
        """
        Check J-Prime (Mind) health via heartbeat file.

        v108.0: Now uses component-specific thresholds and startup grace period awareness.
        J-Prime loads ML models and needs longer timeouts (300s startup, 120s stale threshold).

        v125.0: Enhanced with graceful degradation for optional cross-repo components.
        If J-Prime was never seen (no heartbeat file ever existed), it's marked
        as OPTIONAL_OFFLINE instead of UNHEALTHY - this allows the system to operate
        without J-Prime running.
        """
        status = ComponentHealthStatus(component=TrinityComponent.JARVIS_PRIME)
        checks = []

        # v108.0: Get component-specific threshold
        stale_threshold = self.config.get_heartbeat_threshold(TrinityComponent.JARVIS_PRIME)

        # Heartbeat file check (primary)
        # Support both naming conventions
        heartbeat_files = [
            self.config.trinity_dir / "components" / "jarvis_prime.json",
            self.config.trinity_dir / "components" / "j_prime.json",
            self.config.trinity_dir / "heartbeats" / "jarvis_prime.json",
            self.config.trinity_dir / "heartbeats" / "j_prime.json",
        ]

        # v125.0: Check if any heartbeat file has ever existed
        any_file_exists = any(f.exists() for f in heartbeat_files)

        # v108.0: Pass component-specific threshold
        heartbeat_result = await self._check_heartbeat_file(
            heartbeat_files,
            stale_threshold=stale_threshold,
        )
        checks.append(heartbeat_result)

        if heartbeat_result.success:
            status.heartbeat_age_seconds = heartbeat_result.details.get("age_seconds", 0)
            status.uptime_seconds = heartbeat_result.details.get("uptime_seconds", 0)
            status.metadata = heartbeat_result.details

            # v108.0: Check against component-specific warning threshold
            warning_threshold = stale_threshold * 0.75
            if status.heartbeat_age_seconds and status.heartbeat_age_seconds > warning_threshold:
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY
            status.consecutive_failures = 0
        else:
            # v108.0: CRITICAL FIX - Check if in startup grace period before marking unhealthy
            if self.config.is_in_startup_grace_period(TrinityComponent.JARVIS_PRIME):
                status.status = ComponentStatus.STARTING
                status.last_error = f"J-Prime starting (in grace period) - {heartbeat_result.error}"
                self.log.debug(
                    f"[TrinityHealthMonitor] J-Prime in startup grace period, "
                    f"not marking as unhealthy"
                )
            elif not any_file_exists:
                # v125.0: No heartbeat file ever existed - component not configured/installed
                status.status = ComponentStatus.OPTIONAL_OFFLINE
                status.last_error = "J-Prime not running (optional cross-repo component)"
                self.log.debug(
                    f"[TrinityHealthMonitor] v125.0: J-Prime heartbeat never seen, "
                    f"marking as OPTIONAL_OFFLINE for graceful degradation"
                )
            else:
                # v125.0: Heartbeat file existed but is now stale - actual failure
                status.consecutive_failures += 1
                status.last_error = heartbeat_result.error
                status.status = ComponentStatus.UNHEALTHY

        status.check_results = checks
        status.last_check = time.time()
        return status

    async def _check_reactor_core(self) -> ComponentHealthStatus:
        """
        Check Reactor-Core (Nerves) health via heartbeat file.

        v108.0: Now uses component-specific thresholds and startup grace period awareness.
        Reactor-Core has medium startup time (120s startup, 60s stale threshold).

        v125.0: Enhanced with graceful degradation for optional cross-repo components.
        If Reactor-Core was never seen (no heartbeat file ever existed), it's marked
        as OPTIONAL_OFFLINE instead of UNHEALTHY - this allows the system to operate
        without Reactor-Core running.
        """
        status = ComponentHealthStatus(component=TrinityComponent.REACTOR_CORE)
        checks = []

        # v108.0: Get component-specific threshold
        stale_threshold = self.config.get_heartbeat_threshold(TrinityComponent.REACTOR_CORE)

        # Heartbeat file check - check multiple locations
        heartbeat_files = [
            self.config.trinity_dir / "components" / "reactor_core.json",
            self.config.trinity_dir / "heartbeats" / "reactor_core.json",
        ]

        # v125.0: Check if any heartbeat file has ever existed
        any_file_exists = any(f.exists() for f in heartbeat_files)

        # v108.0: Pass component-specific threshold
        heartbeat_result = await self._check_heartbeat_file(
            heartbeat_files,
            stale_threshold=stale_threshold,
        )
        checks.append(heartbeat_result)

        if heartbeat_result.success:
            status.heartbeat_age_seconds = heartbeat_result.details.get("age_seconds", 0)
            status.uptime_seconds = heartbeat_result.details.get("uptime_seconds", 0)
            status.metadata = heartbeat_result.details

            # v108.0: Check against component-specific warning threshold
            warning_threshold = stale_threshold * 0.75
            if status.heartbeat_age_seconds and status.heartbeat_age_seconds > warning_threshold:
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY
            status.consecutive_failures = 0
        else:
            # v108.0: CRITICAL FIX - Check if in startup grace period before marking unhealthy
            if self.config.is_in_startup_grace_period(TrinityComponent.REACTOR_CORE):
                status.status = ComponentStatus.STARTING
                status.last_error = f"Reactor-Core starting (in grace period) - {heartbeat_result.error}"
                self.log.debug(
                    f"[TrinityHealthMonitor] Reactor-Core in startup grace period, "
                    f"not marking as unhealthy"
                )
            elif not any_file_exists:
                # v125.0: No heartbeat file ever existed - component not configured/installed
                status.status = ComponentStatus.OPTIONAL_OFFLINE
                status.last_error = "Reactor-Core not running (optional cross-repo component)"
                self.log.debug(
                    f"[TrinityHealthMonitor] v125.0: Reactor-Core heartbeat never seen, "
                    f"marking as OPTIONAL_OFFLINE for graceful degradation"
                )
            else:
                # v125.0: Heartbeat file existed but is now stale - actual failure
                status.consecutive_failures += 1
                status.last_error = heartbeat_result.error
                status.status = ComponentStatus.UNHEALTHY

        status.check_results = checks
        status.last_check = time.time()
        return status

    async def _check_coding_council(self) -> ComponentHealthStatus:
        """
        Check Coding Council health via heartbeat file.

        v108.0: Now uses component-specific thresholds and startup grace period awareness.
        Coding Council has fast startup (30s startup, 30s stale threshold).

        v125.0: Enhanced with graceful degradation for optional cross-repo components.
        """
        status = ComponentHealthStatus(component=TrinityComponent.CODING_COUNCIL)
        checks = []

        # v108.0: Get component-specific threshold
        stale_threshold = self.config.get_heartbeat_threshold(TrinityComponent.CODING_COUNCIL)

        # Heartbeat file check - check multiple locations
        heartbeat_files = [
            self.config.trinity_dir / "components" / "coding_council.json",
            self.config.trinity_dir / "heartbeats" / "coding_council.json",
        ]

        # v125.0: Check if any heartbeat file has ever existed
        any_file_exists = any(f.exists() for f in heartbeat_files)

        # v108.0: Pass component-specific threshold
        heartbeat_result = await self._check_heartbeat_file(
            heartbeat_files,
            stale_threshold=stale_threshold,
        )
        checks.append(heartbeat_result)

        if heartbeat_result.success:
            status.heartbeat_age_seconds = heartbeat_result.details.get("age_seconds", 0)
            status.metadata = heartbeat_result.details

            # v108.0: Check against component-specific warning threshold
            warning_threshold = stale_threshold * 0.75
            if status.heartbeat_age_seconds and status.heartbeat_age_seconds > warning_threshold:
                status.status = ComponentStatus.DEGRADED
            else:
                status.status = ComponentStatus.HEALTHY
            status.consecutive_failures = 0
        else:
            # v108.0: CRITICAL FIX - Check if in startup grace period before marking unhealthy
            if self.config.is_in_startup_grace_period(TrinityComponent.CODING_COUNCIL):
                status.status = ComponentStatus.STARTING
                status.last_error = f"Coding Council starting (in grace period) - {heartbeat_result.error}"
                self.log.debug(
                    f"[TrinityHealthMonitor] Coding Council in startup grace period, "
                    f"not marking as unhealthy"
                )
            elif not any_file_exists:
                # v125.0: No heartbeat file ever existed - component not configured/installed
                status.status = ComponentStatus.OPTIONAL_OFFLINE
                status.last_error = "Coding Council not running (optional component)"
                self.log.debug(
                    f"[TrinityHealthMonitor] v125.0: Coding Council heartbeat never seen, "
                    f"marking as OPTIONAL_OFFLINE for graceful degradation"
                )
            else:
                # v125.0: Heartbeat file existed but is now stale - actual failure
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

    async def _check_readiness_endpoint(
        self,
        url: str,
        timeout: float = 5.0,
    ) -> HealthCheckResult:
        """
        v94.0: Check a readiness endpoint.

        Unlike _check_http_endpoint, this method:
        1. Treats 503 as "not ready yet" rather than failure
        2. Extracts readiness-specific data from response
        3. Returns details even on non-200 responses
        """
        result = HealthCheckResult(check_type="readiness", success=False)
        start_time = time.time()

        try:
            import aiohttp

            if self._http_session is None or self._http_session.closed:
                self._http_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                )

            async with self._http_session.get(url) as response:
                result.response_time_ms = (time.time() - start_time) * 1000

                # Try to parse JSON response body regardless of status code
                try:
                    result.details = await response.json()
                except Exception:
                    result.details = {}

                if response.status == 200:
                    result.success = True
                    # Check if response indicates ready
                    if result.details.get("ready", True) is False:
                        result.success = False
                        result.error = "Endpoint returned ready=false"
                elif response.status == 503:
                    # 503 = Service Unavailable = Not ready yet
                    result.success = False
                    result.error = f"HTTP 503 (not ready)"
                    # Include phase info if available
                    phase = result.details.get("phase", "unknown")
                    progress = result.details.get("progress_percent", 0)
                    result.error = f"HTTP 503 (phase={phase}, progress={progress}%)"
                else:
                    result.success = False
                    result.error = f"HTTP {response.status}"

        except asyncio.TimeoutError:
            result.success = False
            result.error = f"Timeout after {timeout}s"
            result.response_time_ms = timeout * 1000
        except Exception as e:
            error_str = str(e)
            if "ConnectionRefusedError" in error_str or "Cannot connect" in error_str:
                result.error = "Connection refused (service not running)"
            else:
                result.error = error_str
            result.response_time_ms = (time.time() - start_time) * 1000

        result.timestamp = time.time()
        return result

    async def _check_heartbeat_file(
        self,
        file_paths: List[Path],
        stale_threshold: Optional[float] = None,
    ) -> HealthCheckResult:
        """
        Check heartbeat file(s) for freshness.

        v108.0: Now accepts component-specific stale_threshold parameter.

        Args:
            file_paths: List of potential heartbeat file paths to check
            stale_threshold: Component-specific staleness threshold (optional)
                             Falls back to config.max_heartbeat_age_seconds if not provided
        """
        result = HealthCheckResult(check_type="heartbeat")
        start_time = time.time()

        # v108.0: Use component-specific threshold if provided
        effective_threshold = stale_threshold or self.config.max_heartbeat_age_seconds

        for file_path in file_paths:
            try:
                if not file_path.exists():
                    continue

                with open(file_path) as f:
                    data = json.load(f)

                heartbeat_ts = data.get("timestamp", 0)
                age_seconds = time.time() - heartbeat_ts

                if age_seconds < effective_threshold:
                    result.success = True
                    result.details = {
                        "file": str(file_path),
                        "age_seconds": age_seconds,
                        "uptime_seconds": data.get("uptime_seconds", 0),
                        "status": data.get("status", "unknown"),
                        "stale_threshold": effective_threshold,  # v108.0: Include threshold for debugging
                        **{k: v for k, v in data.items() if k not in ("timestamp", "uptime_seconds", "status")},
                    }
                    result.response_time_ms = (time.time() - start_time) * 1000
                    return result
                else:
                    # File exists but is stale
                    result.error = f"Heartbeat stale ({age_seconds:.1f}s > {effective_threshold}s)"
                    # v108.0: Store the age for callers to use in startup grace period checks
                    result.details = {
                        "file": str(file_path),
                        "age_seconds": age_seconds,
                        "stale_threshold": effective_threshold,
                    }

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
        """
        Calculate weighted health score (0.0 - 1.0).

        v125.0: Enhanced to handle optional offline components.
        Components with status OPTIONAL_OFFLINE or UNAVAILABLE are excluded
        from the calculation - they don't count against the health score.
        This allows graceful degradation when optional cross-repo components
        are not running.
        """
        if not snapshot.components:
            return 0.0

        total_weight = 0.0
        weighted_score = 0.0

        for component, status in snapshot.components.items():
            weight = self.config.component_weights.get(component, 0.5)

            # v125.0: Skip optional offline components - they don't count against health
            if status.status in (ComponentStatus.OPTIONAL_OFFLINE, ComponentStatus.UNAVAILABLE):
                self.log.debug(
                    f"[TrinityHealthMonitor] v125.0: Excluding {component.value} "
                    f"from health score (status={status.status.value})"
                )
                continue

            total_weight += weight

            if status.status == ComponentStatus.HEALTHY:
                weighted_score += weight * 1.0
            elif status.status == ComponentStatus.DEGRADED:
                weighted_score += weight * 0.5
            elif status.status == ComponentStatus.STARTING:
                weighted_score += weight * 0.7
            elif status.status == ComponentStatus.STOPPED:
                # v125.0: STOPPED is intentional - treat as neutral (0.8)
                weighted_score += weight * 0.8
            # UNHEALTHY, UNKNOWN = 0.0

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
        """
        Write current health status to file for other components to read.

        v117.5: Now writes to both:
        1. ~/.jarvis/trinity/health_status.json (original location)
        2. ~/.jarvis/trinity/state/health.json (shared cross-repo state directory)

        This enables cross-repo health sync where any repo can read the
        aggregated health state from the shared state directory.
        """
        if not self._latest_snapshot:
            return

        health_data = self._latest_snapshot.to_dict()

        # v117.5: Add repo identifier for cross-repo sync
        health_data["source_repo"] = "jarvis-body"
        health_data["sync_version"] = "117.5"

        status_files = [
            self.config.trinity_dir / "health_status.json",  # Original
            Path.home() / ".jarvis" / "trinity" / "state" / "health.json",  # Cross-repo
        ]

        for status_file in status_files:
            try:
                import tempfile

                status_file.parent.mkdir(parents=True, exist_ok=True)

                # Atomic write
                tmp_fd, tmp_name = tempfile.mkstemp(
                    dir=status_file.parent,
                    prefix=".health_status.",
                    suffix=".tmp"
                )

                with os.fdopen(tmp_fd, 'w') as tmp_file:
                    json.dump(health_data, tmp_file, indent=2)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())

                os.replace(tmp_name, status_file)

            except Exception as e:
                self.log.debug(f"[TrinityHealthMonitor] Failed to write {status_file}: {e}")


# =============================================================================
# Global Instance
# =============================================================================

_health_monitor: Optional[TrinityHealthMonitor] = None


async def get_trinity_health_monitor(
    config: Optional[TrinityHealthConfig] = None,
) -> TrinityHealthMonitor:
    """
    Get or create the global Trinity health monitor instance.

    v108.0: If no config is provided, uses TrinityOrchestrationConfig for unified settings.
    """
    global _health_monitor

    if _health_monitor is None:
        # v108.0: Use orchestration config if no config provided
        if config is None:
            config = TrinityHealthConfig.from_orchestration_config()
        _health_monitor = TrinityHealthMonitor(config=config)

    return _health_monitor


async def start_trinity_health_monitoring(
    config: Optional[TrinityHealthConfig] = None,
) -> TrinityHealthMonitor:
    """
    Start Trinity health monitoring.

    v108.0: Uses TrinityOrchestrationConfig for unified settings if no config provided.
    """
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


def load_component_startup_times() -> Dict[str, float]:
    """
    v108.0: Load component startup times from file system.

    This allows the health monitor to respect startup grace periods
    even if it was started after the components.

    Returns:
        Dictionary mapping component names to their startup timestamps
    """
    startup_times: Dict[str, float] = {}

    try:
        from backend.core.trinity_orchestration_config import get_orchestration_config
        orch_config = get_orchestration_config()
        components_dir = orch_config.components_dir

        import json
        for startup_file in components_dir.glob("*_startup.json"):
            try:
                data = json.loads(startup_file.read_text())
                service_name = data.get("service_name")
                startup_time = data.get("startup_time")
                if service_name and startup_time:
                    startup_times[service_name] = startup_time
            except Exception:
                pass

    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[TrinityHealthMonitor] Error loading startup times: {e}")

    return startup_times
