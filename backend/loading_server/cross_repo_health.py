"""
Cross-Repo Health Aggregator for JARVIS Loading Server v212.0
=============================================================

Unified health aggregation across JARVIS, JARVIS-Prime, and Reactor-Core.

Features:
- Circuit breaker state tracking
- Health score aggregation (0-100)
- Component degradation detection
- Anomaly detection
- Health trend analysis
- HTTP and file-based health checks
- Heartbeat file monitoring

Usage:
    from backend.loading_server.cross_repo_health import CrossRepoHealthAggregator

    aggregator = CrossRepoHealthAggregator()
    await aggregator.initialize()
    health = await aggregator.get_unified_health()

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

logger = logging.getLogger("LoadingServer.Health")


class HealthState(Enum):
    """Health state enumeration."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class CircuitState(Enum):
    """Circuit breaker state."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for health check calls.

    Prevents cascade failures by temporarily blocking calls to failing services.
    """

    name: str
    threshold: int = 5  # Failures before opening
    timeout: float = 30.0  # Seconds before half-open
    half_open_successes: int = 2  # Successes needed to close

    _state: CircuitState = field(init=False, default=CircuitState.CLOSED)
    _failure_count: int = field(init=False, default=0)
    _success_count: int = field(init=False, default=0)
    _last_failure_time: float = field(init=False, default=0.0)

    @property
    def state(self) -> CircuitState:
        """Get current state, potentially transitioning from OPEN to HALF_OPEN."""
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.debug(f"[CircuitBreaker:{self.name}] Transitioning to HALF_OPEN")
        return self._state

    def record_success(self) -> None:
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.half_open_successes:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info(f"[CircuitBreaker:{self.name}] Recovered -> CLOSED")
        elif self._state == CircuitState.CLOSED:
            # Decay failures on success
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] Failed in HALF_OPEN -> OPEN")
        elif self._failure_count >= self.threshold:
            self._state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] OPENED after {self._failure_count} failures")

    def can_execute(self) -> bool:
        """Check if a call can be made."""
        state = self.state  # May trigger OPEN -> HALF_OPEN transition
        return state != CircuitState.OPEN


@dataclass
class ComponentHealth:
    """Health status for a single component."""

    name: str
    health_score: float = 0.0  # 0-100
    state: HealthState = HealthState.UNKNOWN
    last_check_time: float = 0.0
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "health": self.health_score,
            "state": self.state.value,
            "response_time_ms": self.response_time_ms,
            "error": self.error,
            "last_check": self.last_check_time,
            "metadata": self.metadata,
        }


@dataclass
class CrossRepoHealthAggregator:
    """
    Unified health aggregation across JARVIS, J-Prime, and Reactor-Core.

    Features:
    - HTTP health endpoint checking
    - Heartbeat file monitoring
    - Circuit breaker pattern for failing services
    - Health score aggregation (0-100)
    - Trend analysis and anomaly detection
    """

    # Configuration
    jarvis_home: Path = field(
        default_factory=lambda: Path.home() / ".jarvis"
    )
    health_check_timeout: float = 5.0
    stale_threshold: float = 30.0  # Heartbeat stale after 30s

    # Component configuration
    components: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # State
    _circuit_breakers: Dict[str, CircuitBreaker] = field(init=False, default_factory=dict)
    _health_cache: Dict[str, ComponentHealth] = field(init=False, default_factory=dict)
    _health_history: Dict[str, Deque] = field(init=False, default_factory=dict)
    _max_history: int = field(init=False, default=60)
    _initialized: bool = field(init=False, default=False)

    def __post_init__(self):
        """Initialize default components if not provided."""
        if not self.components:
            self.components = {
                "jarvis_body": {
                    "port": 8010,
                    "health_endpoint": "/health",
                    "heartbeat_file": "jarvis_body.json",
                    "critical": True,
                },
                "jarvis_prime": {
                    "port": 8000,
                    "health_endpoint": "/health",
                    "heartbeat_file": "jarvis_prime.json",
                    "critical": False,
                },
                "reactor_core": {
                    "port": 8090,
                    "health_endpoint": "/health",
                    "heartbeat_file": "reactor_core.json",
                    "critical": False,
                },
                "coding_council": {
                    "heartbeat_file": "coding_council.json",
                    "critical": False,
                },
            }

    async def initialize(self) -> None:
        """Initialize health aggregator."""
        # Create circuit breakers for each component
        for name in self.components:
            self._circuit_breakers[name] = CircuitBreaker(name=name)
            self._health_history[name] = deque(maxlen=self._max_history)

        self._initialized = True
        logger.info(f"[HealthAggregator] Initialized with {len(self.components)} components")

    @property
    def heartbeat_dir(self) -> Path:
        """Get the heartbeat directory path."""
        return self.jarvis_home / "trinity" / "components"

    async def _check_http_health(self, name: str, port: int, endpoint: str) -> ComponentHealth:
        """
        Check component health via HTTP endpoint.

        Args:
            name: Component name
            port: Port number
            endpoint: Health endpoint path

        Returns:
            ComponentHealth with check results
        """
        cb = self._circuit_breakers.get(name)
        if cb and not cb.can_execute():
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error="Circuit breaker open",
            )

        start = time.time()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("localhost", port),
                timeout=self.health_check_timeout,
            )

            request = f"GET {endpoint} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            writer.write(request.encode())
            await writer.drain()

            response = await asyncio.wait_for(
                reader.read(4096), timeout=self.health_check_timeout
            )
            writer.close()
            await writer.wait_closed()

            response_time = (time.time() - start) * 1000
            response_str = response.decode("utf-8", errors="ignore")

            if "HTTP/1.1 200" in response_str or "HTTP/1.0 200" in response_str:
                if cb:
                    cb.record_success()

                # Try to extract JSON body
                metadata = {}
                body_start = response_str.find("\r\n\r\n")
                if body_start > 0:
                    try:
                        metadata = json.loads(response_str[body_start + 4:])
                    except json.JSONDecodeError:
                        pass

                return ComponentHealth(
                    name=name,
                    health_score=100.0,
                    state=HealthState.HEALTHY,
                    response_time_ms=response_time,
                    last_check_time=time.time(),
                    metadata=metadata,
                )
            else:
                if cb:
                    cb.record_failure()
                return ComponentHealth(
                    name=name,
                    health_score=0.0,
                    state=HealthState.UNHEALTHY,
                    response_time_ms=response_time,
                    error="Non-200 response",
                    last_check_time=time.time(),
                )

        except asyncio.TimeoutError:
            if cb:
                cb.record_failure()
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNHEALTHY,
                error="Health check timeout",
                last_check_time=time.time(),
            )
        except Exception as e:
            if cb:
                cb.record_failure()
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error=str(e),
                last_check_time=time.time(),
            )

    async def _check_heartbeat_health(self, name: str, heartbeat_file: str) -> ComponentHealth:
        """
        Check component health via heartbeat file.

        Args:
            name: Component name
            heartbeat_file: Heartbeat filename

        Returns:
            ComponentHealth with check results
        """
        heartbeat_path = self.heartbeat_dir / heartbeat_file

        if not heartbeat_path.exists():
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error="Heartbeat file not found",
                last_check_time=time.time(),
            )

        try:
            content = heartbeat_path.read_text()
            data = json.loads(content)
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            if age < 10:
                health_score = 100.0
                state = HealthState.HEALTHY
            elif age < self.stale_threshold:
                health_score = 70.0
                state = HealthState.DEGRADED
            elif age < self.stale_threshold * 2:
                health_score = 30.0
                state = HealthState.UNHEALTHY
            else:
                health_score = 0.0
                state = HealthState.CRITICAL

            return ComponentHealth(
                name=name,
                health_score=health_score,
                state=state,
                last_check_time=time.time(),
                metadata={"heartbeat_age": age, **data},
            )

        except json.JSONDecodeError as e:
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error=f"Invalid heartbeat JSON: {e}",
                last_check_time=time.time(),
            )
        except Exception as e:
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error=str(e),
                last_check_time=time.time(),
            )

    async def check_component(self, name: str) -> ComponentHealth:
        """
        Check health of a specific component.

        Uses HTTP check if port is configured, otherwise falls back to heartbeat.

        Args:
            name: Component name

        Returns:
            ComponentHealth with check results
        """
        config = self.components.get(name)
        if not config:
            return ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error="Unknown component",
            )

        # Try HTTP first if configured
        if "port" in config:
            health = await self._check_http_health(
                name,
                config["port"],
                config.get("health_endpoint", "/health"),
            )

            # If HTTP fails, try heartbeat as fallback
            if health.state in (HealthState.UNKNOWN, HealthState.UNHEALTHY):
                if "heartbeat_file" in config:
                    heartbeat_health = await self._check_heartbeat_health(
                        name, config["heartbeat_file"]
                    )
                    # Use heartbeat if it's healthier
                    if heartbeat_health.health_score > health.health_score:
                        health = heartbeat_health
        elif "heartbeat_file" in config:
            health = await self._check_heartbeat_health(name, config["heartbeat_file"])
        else:
            health = ComponentHealth(
                name=name,
                health_score=0.0,
                state=HealthState.UNKNOWN,
                error="No health check method configured",
            )

        # Update cache and history
        self._health_cache[name] = health
        self._health_history[name].append(health.health_score)

        return health

    async def get_unified_health(self) -> Dict[str, Any]:
        """
        Get unified health status across all repos.

        Returns:
            Dict with:
            - overall_health: 0-100 score
            - state: healthy, degraded, critical
            - components: Per-component health
            - circuit_breakers: Circuit breaker states
            - trends: Health trend analysis
        """
        if not self._initialized:
            await self.initialize()

        # Check all components in parallel
        tasks = [self.check_component(name) for name in self.components]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        components_health: Dict[str, ComponentHealth] = {}
        for i, name in enumerate(self.components):
            result = results[i]
            if isinstance(result, Exception):
                components_health[name] = ComponentHealth(
                    name=name,
                    health_score=0.0,
                    state=HealthState.CRITICAL,
                    error=str(result),
                )
            elif isinstance(result, ComponentHealth):
                components_health[name] = result
            else:
                components_health[name] = ComponentHealth(
                    name=name,
                    health_score=0.0,
                    state=HealthState.UNKNOWN,
                    error="Invalid result type",
                )

        # Calculate weighted overall health
        total_weight = 0
        weighted_health = 0
        critical_unhealthy = False

        for name, config in self.components.items():
            health = components_health[name]
            weight = 2.0 if config.get("critical") else 1.0
            total_weight += weight
            weighted_health += health.health_score * weight

            if config.get("critical") and health.state == HealthState.CRITICAL:
                critical_unhealthy = True

        overall_health = weighted_health / total_weight if total_weight > 0 else 0

        # Determine overall state
        if critical_unhealthy:
            overall_state = HealthState.CRITICAL
        elif overall_health >= 80:
            overall_state = HealthState.HEALTHY
        elif overall_health >= 50:
            overall_state = HealthState.DEGRADED
        else:
            overall_state = HealthState.UNHEALTHY

        # Calculate trends
        trends = {}
        for name, history in self._health_history.items():
            if len(history) >= 5:
                recent = list(history)[-5:]
                older = list(history)[-10:-5] if len(history) >= 10 else list(history)[:5]
                recent_avg = sum(recent) / len(recent)
                older_avg = sum(older) / len(older) if older else recent_avg

                if recent_avg > older_avg + 5:
                    trends[name] = "improving"
                elif recent_avg < older_avg - 5:
                    trends[name] = "degrading"
                else:
                    trends[name] = "stable"
            else:
                trends[name] = "unknown"

        return {
            "overall_health": round(overall_health, 1),
            "state": overall_state.value,
            "components": {
                name: health.to_dict() for name, health in components_health.items()
            },
            "circuit_breakers": {
                name: cb.state.value for name, cb in self._circuit_breakers.items()
            },
            "trends": trends,
            "timestamp": time.time(),
        }

    def get_cached_health(self, name: str) -> Optional[ComponentHealth]:
        """Get cached health for a component (no new check)."""
        return self._health_cache.get(name)

    def get_circuit_breaker_state(self, name: str) -> Optional[CircuitState]:
        """Get circuit breaker state for a component."""
        cb = self._circuit_breakers.get(name)
        return cb.state if cb else None

    def reset_circuit_breaker(self, name: str) -> bool:
        """
        Manually reset a circuit breaker to closed state.

        Args:
            name: Component name

        Returns:
            True if reset successful
        """
        cb = self._circuit_breakers.get(name)
        if cb:
            cb._state = CircuitState.CLOSED
            cb._failure_count = 0
            cb._success_count = 0
            logger.info(f"[HealthAggregator] Reset circuit breaker for {name}")
            return True
        return False
