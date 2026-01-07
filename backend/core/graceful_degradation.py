"""
Graceful Degradation System v79.0
=================================

Provides intelligent fallback routing when Trinity components fail.
Ensures JARVIS continues operating even when subsystems are unavailable.

FALLBACK CHAIN:
    Primary: Local JARVIS-Prime (Mind) → Fast, free, private
    ↓ (if unavailable)
    Secondary: Cloud Claude API → Reliable, but costs money
    ↓ (if unavailable)
    Tertiary: Cached responses → Limited, but always available
    ↓ (if unavailable)
    Final: Graceful error message → Never crashes

FEATURES:
    - Automatic fallback on component failure
    - Health-based routing decisions
    - Cost-aware routing (prefer local when healthy)
    - Circuit breaker integration
    - Metrics and alerting
    - Manual override capability

USAGE:
    from backend.core.graceful_degradation import GracefulDegradation, InferenceTarget

    degradation = GracefulDegradation()

    # Get best available target
    target = await degradation.get_best_target(request_type="inference")

    # Execute with automatic fallback
    result = await degradation.execute_with_fallback(
        primary_fn=call_local_prime,
        fallback_fn=call_cloud_api,
        request=request,
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Generic

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# IMPORTS
# =============================================================================

try:
    from backend.core.trinity_config import get_config, ComponentType, ComponentHealth
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False


# =============================================================================
# ENUMS AND DATA STRUCTURES
# =============================================================================


class InferenceTarget(Enum):
    """Available inference targets."""
    LOCAL_PRIME = "local_prime"      # JARVIS-Prime local model
    CLOUD_CLAUDE = "cloud_claude"    # Anthropic Claude API
    CLOUD_OPENAI = "cloud_openai"    # OpenAI API (backup)
    CACHED = "cached"                # Cached responses
    DEGRADED = "degraded"            # Minimal functionality


class TargetHealth(Enum):
    """Health status of inference targets."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class FallbackReason(Enum):
    """Reasons for falling back to alternative target."""
    NONE = "none"
    PRIMARY_UNHEALTHY = "primary_unhealthy"
    PRIMARY_TIMEOUT = "primary_timeout"
    PRIMARY_ERROR = "primary_error"
    PRIMARY_OVERLOADED = "primary_overloaded"
    CIRCUIT_OPEN = "circuit_open"
    COST_LIMIT = "cost_limit"
    MANUAL_OVERRIDE = "manual_override"


@dataclass
class TargetStatus:
    """Status of an inference target."""
    target: InferenceTarget
    health: TargetHealth = TargetHealth.UNKNOWN
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    avg_latency_ms: float = 0.0
    circuit_open: bool = False
    enabled: bool = True

    def record_success(self, latency_ms: float) -> None:
        """Record successful request."""
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.total_requests += 1
        # Exponential moving average for latency
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = 0.9 * self.avg_latency_ms + 0.1 * latency_ms
        self._update_health()

    def record_failure(self) -> None:
        """Record failed request."""
        self.last_failure = time.time()
        self.consecutive_failures += 1
        self.total_requests += 1
        self.total_failures += 1
        self._update_health()

    def _update_health(self) -> None:
        """Update health based on recent performance."""
        if self.consecutive_failures >= 5:
            self.health = TargetHealth.UNHEALTHY
            self.circuit_open = True
        elif self.consecutive_failures >= 2:
            self.health = TargetHealth.DEGRADED
        elif self.last_success > 0:
            self.health = TargetHealth.HEALTHY
            # Reset circuit if we've recovered
            if time.time() - self.last_failure > 30:
                self.circuit_open = False


@dataclass
class FallbackResult(Generic[T]):
    """Result of a fallback operation."""
    success: bool
    value: Optional[T] = None
    target_used: InferenceTarget = InferenceTarget.DEGRADED
    fallback_reason: FallbackReason = FallbackReason.NONE
    latency_ms: float = 0.0
    error: Optional[str] = None
    attempts: int = 0


@dataclass
class RoutingDecision:
    """Decision about which target to use."""
    target: InferenceTarget
    reason: str
    confidence: float = 1.0
    alternatives: List[InferenceTarget] = field(default_factory=list)


# =============================================================================
# GRACEFUL DEGRADATION SYSTEM
# =============================================================================


class GracefulDegradation:
    """
    Intelligent fallback routing for Trinity components.

    Ensures JARVIS remains operational even when subsystems fail.
    """

    def __init__(self):
        self._targets: Dict[InferenceTarget, TargetStatus] = {}
        self._fallback_chain: List[InferenceTarget] = [
            InferenceTarget.LOCAL_PRIME,
            InferenceTarget.CLOUD_CLAUDE,
            InferenceTarget.CLOUD_OPENAI,
            InferenceTarget.CACHED,
            InferenceTarget.DEGRADED,
        ]
        self._manual_override: Optional[InferenceTarget] = None
        self._lock = asyncio.Lock()
        self._init_targets()

    def _init_targets(self) -> None:
        """Initialize target statuses."""
        for target in InferenceTarget:
            self._targets[target] = TargetStatus(target=target)

        # Local Prime enabled by default if available
        self._targets[InferenceTarget.LOCAL_PRIME].enabled = True
        # Cloud always available as fallback
        self._targets[InferenceTarget.CLOUD_CLAUDE].enabled = True
        # OpenAI as secondary cloud backup
        self._targets[InferenceTarget.CLOUD_OPENAI].enabled = True
        # Cached always available
        self._targets[InferenceTarget.CACHED].enabled = True
        # Degraded mode always available
        self._targets[InferenceTarget.DEGRADED].enabled = True

    async def get_best_target(
        self,
        request_type: str = "inference",
        prefer_local: bool = True,
    ) -> RoutingDecision:
        """
        Get the best available inference target.

        Args:
            request_type: Type of request (inference, embedding, etc.)
            prefer_local: Whether to prefer local over cloud

        Returns:
            RoutingDecision with target and reason
        """
        async with self._lock:
            # Check for manual override
            if self._manual_override:
                override_status = self._targets[self._manual_override]
                if override_status.enabled and not override_status.circuit_open:
                    return RoutingDecision(
                        target=self._manual_override,
                        reason="manual_override",
                        confidence=1.0,
                        alternatives=self._get_alternatives(self._manual_override),
                    )

            # Try targets in fallback chain order
            for target in self._fallback_chain:
                status = self._targets[target]

                # Skip disabled targets
                if not status.enabled:
                    continue

                # Skip targets with open circuit
                if status.circuit_open:
                    # Check if circuit should be reset (half-open)
                    if time.time() - status.last_failure > 30:
                        status.circuit_open = False
                        logger.info(f"[Degradation] Circuit half-open for {target.value}")
                    else:
                        continue

                # Skip unhealthy targets unless it's the last resort
                if status.health == TargetHealth.UNHEALTHY:
                    if target != InferenceTarget.DEGRADED:
                        continue

                # Found a suitable target
                return RoutingDecision(
                    target=target,
                    reason=self._get_selection_reason(target, status),
                    confidence=self._calculate_confidence(status),
                    alternatives=self._get_alternatives(target),
                )

            # Fallback to degraded mode
            return RoutingDecision(
                target=InferenceTarget.DEGRADED,
                reason="all_targets_unavailable",
                confidence=0.1,
                alternatives=[],
            )

    def _get_selection_reason(self, target: InferenceTarget, status: TargetStatus) -> str:
        """Get reason for selecting a target."""
        if target == InferenceTarget.LOCAL_PRIME:
            if status.health == TargetHealth.HEALTHY:
                return "local_healthy"
            return "local_available"
        elif target == InferenceTarget.CLOUD_CLAUDE:
            return "cloud_fallback"
        elif target == InferenceTarget.CACHED:
            return "cache_fallback"
        else:
            return "degraded_fallback"

    def _calculate_confidence(self, status: TargetStatus) -> float:
        """Calculate confidence in a target."""
        if status.health == TargetHealth.HEALTHY:
            return 1.0
        elif status.health == TargetHealth.DEGRADED:
            return 0.7
        elif status.health == TargetHealth.UNKNOWN:
            return 0.5
        else:
            return 0.1

    def _get_alternatives(self, selected: InferenceTarget) -> List[InferenceTarget]:
        """Get alternative targets after the selected one."""
        try:
            idx = self._fallback_chain.index(selected)
            return self._fallback_chain[idx + 1:]
        except ValueError:
            return []

    async def execute_with_fallback(
        self,
        primary_fn: Callable[..., Awaitable[T]],
        fallback_fn: Optional[Callable[..., Awaitable[T]]] = None,
        cached_fn: Optional[Callable[..., Awaitable[T]]] = None,
        default_value: Optional[T] = None,
        timeout: float = 30.0,
        *args,
        **kwargs,
    ) -> FallbackResult[T]:
        """
        Execute a request with automatic fallback on failure.

        Args:
            primary_fn: Primary function to try (e.g., local Prime)
            fallback_fn: Fallback function (e.g., cloud API)
            cached_fn: Cache lookup function
            default_value: Default value if all fail
            timeout: Timeout for each attempt
            *args, **kwargs: Arguments to pass to functions

        Returns:
            FallbackResult with value and metadata
        """
        start_time = time.time()
        attempts = 0

        # Try primary (Local Prime)
        primary_target = InferenceTarget.LOCAL_PRIME
        primary_status = self._targets[primary_target]

        if primary_status.enabled and not primary_status.circuit_open:
            attempts += 1
            try:
                result = await asyncio.wait_for(
                    primary_fn(*args, **kwargs),
                    timeout=timeout,
                )
                latency_ms = (time.time() - start_time) * 1000
                primary_status.record_success(latency_ms)

                return FallbackResult(
                    success=True,
                    value=result,
                    target_used=primary_target,
                    fallback_reason=FallbackReason.NONE,
                    latency_ms=latency_ms,
                    attempts=attempts,
                )
            except asyncio.TimeoutError:
                primary_status.record_failure()
                logger.warning(f"[Degradation] Primary timeout after {timeout}s")
                fallback_reason = FallbackReason.PRIMARY_TIMEOUT
            except Exception as e:
                primary_status.record_failure()
                logger.warning(f"[Degradation] Primary error: {e}")
                fallback_reason = FallbackReason.PRIMARY_ERROR
        else:
            fallback_reason = FallbackReason.CIRCUIT_OPEN if primary_status.circuit_open else FallbackReason.PRIMARY_UNHEALTHY

        # Try fallback (Cloud API)
        if fallback_fn:
            fallback_target = InferenceTarget.CLOUD_CLAUDE
            fallback_status = self._targets[fallback_target]

            if fallback_status.enabled and not fallback_status.circuit_open:
                attempts += 1
                try:
                    result = await asyncio.wait_for(
                        fallback_fn(*args, **kwargs),
                        timeout=timeout,
                    )
                    latency_ms = (time.time() - start_time) * 1000
                    fallback_status.record_success(latency_ms)

                    return FallbackResult(
                        success=True,
                        value=result,
                        target_used=fallback_target,
                        fallback_reason=fallback_reason,
                        latency_ms=latency_ms,
                        attempts=attempts,
                    )
                except Exception as e:
                    fallback_status.record_failure()
                    logger.warning(f"[Degradation] Fallback error: {e}")

        # Try cache
        if cached_fn:
            cached_target = InferenceTarget.CACHED
            attempts += 1
            try:
                result = await cached_fn(*args, **kwargs)
                if result is not None:
                    latency_ms = (time.time() - start_time) * 1000
                    return FallbackResult(
                        success=True,
                        value=result,
                        target_used=cached_target,
                        fallback_reason=fallback_reason,
                        latency_ms=latency_ms,
                        attempts=attempts,
                    )
            except Exception as e:
                logger.debug(f"[Degradation] Cache lookup failed: {e}")

        # Return default value
        latency_ms = (time.time() - start_time) * 1000
        return FallbackResult(
            success=default_value is not None,
            value=default_value,
            target_used=InferenceTarget.DEGRADED,
            fallback_reason=fallback_reason,
            latency_ms=latency_ms,
            error="All targets failed",
            attempts=attempts,
        )

    def set_manual_override(self, target: Optional[InferenceTarget]) -> None:
        """Set manual override for target selection."""
        self._manual_override = target
        if target:
            logger.info(f"[Degradation] Manual override set to {target.value}")
        else:
            logger.info("[Degradation] Manual override cleared")

    def update_target_health(
        self,
        target: InferenceTarget,
        health: TargetHealth,
    ) -> None:
        """Update health status of a target externally."""
        if target in self._targets:
            self._targets[target].health = health
            logger.debug(f"[Degradation] {target.value} health updated to {health.value}")

    def enable_target(self, target: InferenceTarget, enabled: bool = True) -> None:
        """Enable or disable a target."""
        if target in self._targets:
            self._targets[target].enabled = enabled
            logger.info(f"[Degradation] {target.value} {'enabled' if enabled else 'disabled'}")

    def get_status(self) -> Dict[str, Any]:
        """Get status of all targets."""
        return {
            "targets": {
                target.value: {
                    "health": status.health.value,
                    "enabled": status.enabled,
                    "circuit_open": status.circuit_open,
                    "consecutive_failures": status.consecutive_failures,
                    "total_requests": status.total_requests,
                    "total_failures": status.total_failures,
                    "avg_latency_ms": round(status.avg_latency_ms, 2),
                }
                for target, status in self._targets.items()
            },
            "manual_override": self._manual_override.value if self._manual_override else None,
            "fallback_chain": [t.value for t in self._fallback_chain],
        }

    def reset_circuit(self, target: InferenceTarget) -> None:
        """Manually reset circuit breaker for a target."""
        if target in self._targets:
            self._targets[target].circuit_open = False
            self._targets[target].consecutive_failures = 0
            logger.info(f"[Degradation] Circuit reset for {target.value}")


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_degradation: Optional[GracefulDegradation] = None


def get_degradation() -> GracefulDegradation:
    """Get the singleton GracefulDegradation instance."""
    global _degradation
    if _degradation is None:
        _degradation = GracefulDegradation()
    return _degradation
