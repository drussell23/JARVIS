#!/usr/bin/env python3
"""
Advanced Transport Manager
===========================

Enterprise-grade multi-transport communication layer with:
- Health monitoring and circuit breakers
- Smart method selection based on real-time metrics
- Automatic fallback and recovery
- Comprehensive logging and debugging
- Zero hardcoding - fully dynamic configuration
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from backend.core.async_safety import TimeoutConfig, get_shutdown_event

logger = logging.getLogger(__name__)


class TransportMethod(Enum):
    """Available transport methods for screen control"""

    APPLESCRIPT = "applescript"
    HTTP_REST = "http_rest"
    UNIFIED_WEBSOCKET = "unified_websocket"
    SYSTEM_API = "system_api"


class TransportHealth(Enum):
    """Transport health status"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class TransportMetrics:
    """Metrics for a transport method"""

    method: TransportMethod
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0
    last_success_time: Optional[float] = None
    last_failure_time: Optional[float] = None
    consecutive_failures: int = 0
    health: TransportHealth = TransportHealth.UNKNOWN
    enabled: bool = True

    @property
    def success_rate(self) -> float:
        """Calculate success rate"""
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total

    @property
    def avg_latency_ms(self) -> float:
        """Calculate average latency"""
        if self.success_count == 0:
            return 0.0
        return self.total_latency_ms / self.success_count

    def update_success(self, latency_ms: float):
        """Record successful transport"""
        self.success_count += 1
        self.total_latency_ms += latency_ms
        self.last_success_time = time.time()
        self.consecutive_failures = 0
        self._update_health()

    def update_failure(self):
        """Record failed transport"""
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.consecutive_failures += 1
        self._update_health()

    def _update_health(self):
        """Update health status based on metrics"""
        # Circuit breaker: disable after 3 consecutive failures
        if self.consecutive_failures >= 3:
            self.health = TransportHealth.UNHEALTHY
            self.enabled = False
            logger.warning(
                f"[TRANSPORT] {self.method.value} marked UNHEALTHY "
                f"(consecutive failures: {self.consecutive_failures})"
            )
            return

        # Calculate health based on success rate and latency
        if self.success_rate >= 0.9 and self.avg_latency_ms < 1000:
            self.health = TransportHealth.HEALTHY
        elif self.success_rate >= 0.7 or self.avg_latency_ms < 2000:
            self.health = TransportHealth.DEGRADED
        else:
            self.health = TransportHealth.UNHEALTHY

    def reset_circuit_breaker(self):
        """Reset circuit breaker after cooldown"""
        if not self.enabled:
            logger.info(f"[TRANSPORT] Resetting circuit breaker for {self.method.value}")
            self.enabled = True
            self.consecutive_failures = 0
            self.health = TransportHealth.UNKNOWN


@dataclass
class TransportConfig:
    """Configuration for transport methods"""

    # Timeouts (seconds)
    applescript_timeout: float = 5.0
    http_timeout: float = 3.0
    websocket_timeout: float = 2.0
    system_api_timeout: float = 4.0

    # Retry settings
    max_retries: int = 2
    retry_delay_ms: float = 100.0

    # Circuit breaker settings
    circuit_breaker_threshold: int = 3
    circuit_breaker_timeout: float = 30.0

    # Health check settings
    health_check_interval: float = 60.0
    metrics_window_size: int = 100


class TransportManager:
    """
    Advanced transport manager with health monitoring and smart selection.

    Features:
    - Real-time health monitoring for all transports
    - Circuit breaker pattern to prevent cascading failures
    - Smart method selection based on latency and success rate
    - Automatic fallback and recovery
    - Comprehensive metrics and logging
    """

    def __init__(self, config: Optional[TransportConfig] = None):
        self.config = config or TransportConfig()
        self.metrics: Dict[TransportMethod, TransportMetrics] = {
            method: TransportMetrics(method=method) for method in TransportMethod
        }
        self._handlers: Dict[TransportMethod, Callable] = {}
        self._health_check_task: Optional[asyncio.Task] = None
        self._initialized = False

        logger.info("[TRANSPORT] Transport Manager initialized")

    def register_handler(self, method: TransportMethod, handler: Callable):
        """Register a transport method handler"""
        self._handlers[method] = handler
        logger.info(f"[TRANSPORT] Registered handler for {method.value}")

    async def initialize(self):
        """Initialize transport manager and start health monitoring"""
        if self._initialized:
            return

        # Start background health monitoring
        self._health_check_task = asyncio.create_task(self._health_monitor_loop())
        self._initialized = True
        logger.info("[TRANSPORT] Transport Manager started")

    async def execute(
        self, action: str, context: Optional[Dict[str, Any]] = None, **kwargs
    ) -> Dict[str, Any]:
        """
        Execute action using best available transport method.

        Smart selection based on:
        1. Health status (healthy > degraded > unhealthy)
        2. Success rate
        3. Average latency
        4. Recent performance
        """
        context = context or {}

        # Get prioritized list of methods
        methods = self._select_transport_methods()

        # Screen control actions should strongly prefer local execution paths.
        # This avoids unnecessary loopback HTTP calls and prevents accidental recursion.
        if action in ("lock_screen", "unlock_screen") and methods:
            preferred = [
                TransportMethod.APPLESCRIPT,
                TransportMethod.SYSTEM_API,
                TransportMethod.HTTP_REST,
                TransportMethod.UNIFIED_WEBSOCKET,
            ]
            methods = [m for m in preferred if m in methods] + [m for m in methods if m not in preferred]

        if not methods:
            logger.error("[TRANSPORT] No healthy transport methods available")
            return {
                "success": False,
                "error": "no_transport_available",
                "message": "All transport methods are currently unavailable",
            }

        # Try methods in priority order
        last_error = None
        for method in methods:
            if not self.metrics[method].enabled:
                continue

            try:
                logger.info(
                    f"[TRANSPORT] Attempting {action} via {method.value} "
                    f"(health: {self.metrics[method].health.value}, "
                    f"success_rate: {self.metrics[method].success_rate:.2%})"
                )

                start_time = time.time()
                result = await self._execute_with_timeout(method, action, context, **kwargs)
                latency_ms = (time.time() - start_time) * 1000

                if result.get("success"):
                    self.metrics[method].update_success(latency_ms)
                    result["transport_method"] = method.value
                    result["latency_ms"] = latency_ms

                    logger.info(
                        f"[TRANSPORT] ✅ {action} succeeded via {method.value} "
                        f"({latency_ms:.1f}ms)"
                    )
                    return result
                else:
                    self.metrics[method].update_failure()
                    last_error = result.get("error", "unknown_error")

            except asyncio.TimeoutError:
                logger.warning(f"[TRANSPORT] {method.value} timed out")
                self.metrics[method].update_failure()
                last_error = "timeout"
            except Exception as e:
                logger.error(f"[TRANSPORT] {method.value} failed: {e}", exc_info=True)
                self.metrics[method].update_failure()
                last_error = str(e)

        # All methods failed
        return {
            "success": False,
            "error": "all_transports_failed",
            "last_error": last_error,
            "message": "All transport methods failed",
            "attempted_methods": [m.value for m in methods],
        }

    def _select_transport_methods(self) -> List[TransportMethod]:
        """
        Intelligently select and prioritize transport methods.

        Priority factors:
        1. Health status (healthy > degraded > unhealthy)
        2. Success rate (higher is better)
        3. Average latency (lower is better)
        4. Recency of success
        """
        available = [
            (method, metrics)
            for method, metrics in self.metrics.items()
            if metrics.enabled and method in self._handlers
        ]

        if not available:
            return []

        # Score each method
        def score_method(method_metrics_tuple) -> float:
            method, metrics = method_metrics_tuple

            # Health score (0-100)
            health_score = {
                TransportHealth.HEALTHY: 100,
                TransportHealth.DEGRADED: 60,
                TransportHealth.UNHEALTHY: 20,
                TransportHealth.UNKNOWN: 40,
            }[metrics.health]

            # Success rate score (0-100)
            success_score = metrics.success_rate * 100

            # Latency score (0-100, inverse of latency)
            latency_score = max(0, 100 - (metrics.avg_latency_ms / 10))

            # Recency bonus (0-20)
            recency_bonus = 0
            if metrics.last_success_time:
                seconds_since_success = time.time() - metrics.last_success_time
                recency_bonus = max(0, 20 - (seconds_since_success / 10))

            total_score = (
                health_score * 0.4 + success_score * 0.3 + latency_score * 0.2 + recency_bonus * 0.1
            )

            return total_score

        # Sort by score (highest first)
        sorted_methods = sorted(available, key=score_method, reverse=True)

        result = [method for method, _ in sorted_methods]

        logger.debug(
            f"[TRANSPORT] Method priority: "
            f"{', '.join(f'{m.value}({score_method((m, self.metrics[m])):.1f})' for m in result)}"
        )

        return result

    async def _execute_with_timeout(
        self, method: TransportMethod, action: str, context: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Execute transport method with appropriate timeout"""
        timeout_map = {
            TransportMethod.APPLESCRIPT: self.config.applescript_timeout,
            TransportMethod.HTTP_REST: self.config.http_timeout,
            TransportMethod.UNIFIED_WEBSOCKET: self.config.websocket_timeout,
            TransportMethod.SYSTEM_API: self.config.system_api_timeout,
        }

        timeout = timeout_map.get(method, 5.0)
        handler = self._handlers[method]

        # Use asyncio.wait_for for Python 3.10 compatibility
        return await asyncio.wait_for(handler(action, context, **kwargs), timeout=timeout)

    async def _health_monitor_loop(self):
        """Background task to monitor transport health and reset circuit breakers with timeout protection."""
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("TRANSPORT_HEALTH_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("[TRANSPORT] Health monitor stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"[TRANSPORT] Health monitor reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(self.config.health_check_interval)

                # Check and reset circuit breakers
                for method, metrics in self.metrics.items():
                    if not metrics.enabled:
                        # Check if enough time has passed since last failure
                        if metrics.last_failure_time:
                            time_since_failure = time.time() - metrics.last_failure_time
                            if time_since_failure >= self.config.circuit_breaker_timeout:
                                metrics.reset_circuit_breaker()

                # Log health summary
                self._log_health_summary()

            except asyncio.CancelledError:
                logger.info("[TRANSPORT] Health monitor cancelled")
                break
            except Exception as e:
                logger.error(f"[TRANSPORT] Health monitor error: {e}", exc_info=True)

    def _log_health_summary(self):
        """Log summary of transport health"""
        summary_lines = ["[TRANSPORT] Health Summary:"]

        for method, metrics in self.metrics.items():
            if method not in self._handlers:
                continue

            status = (
                "🟢"
                if metrics.health == TransportHealth.HEALTHY
                else "🟡" if metrics.health == TransportHealth.DEGRADED else "🔴"
            )

            summary_lines.append(
                f"  {status} {method.value:20s} | "
                f"Health: {metrics.health.value:10s} | "
                f"Success: {metrics.success_rate:6.1%} | "
                f"Latency: {metrics.avg_latency_ms:6.1f}ms | "
                f"Enabled: {metrics.enabled}"
            )

        logger.info("\n".join(summary_lines))

    def get_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get current metrics for all transport methods"""
        return {
            method.value: {
                "health": metrics.health.value,
                "success_rate": metrics.success_rate,
                "avg_latency_ms": metrics.avg_latency_ms,
                "success_count": metrics.success_count,
                "failure_count": metrics.failure_count,
                "consecutive_failures": metrics.consecutive_failures,
                "enabled": metrics.enabled,
            }
            for method, metrics in self.metrics.items()
        }

    async def shutdown(self):
        """Shutdown transport manager"""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        logger.info("[TRANSPORT] Transport Manager shutdown")


# Global instance
_transport_manager: Optional[TransportManager] = None


def get_transport_manager(config: Optional[TransportConfig] = None) -> TransportManager:
    """Get or create global transport manager instance"""
    global _transport_manager
    if _transport_manager is None:
        _transport_manager = TransportManager(config)
    return _transport_manager
