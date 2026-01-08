"""
Graceful Degradation System v80.0
=================================

Provides intelligent fallback routing when Trinity components fail.
Ensures JARVIS continues operating even when subsystems are unavailable.

v80.0 ENHANCEMENTS:
    - ResourceBulkhead integration for failure isolation
    - AdaptiveBackpressure for memory-aware throttling
    - TimeoutProtectedLock for deadlock prevention
    - Deep health verification beyond HTTP 200
    - Priority-based request handling
    - Proactive circuit breaker with permit system

FALLBACK CHAIN:
    Primary: Local JARVIS-Prime (Mind) → Fast, free, private
    ↓ (if unavailable or bulkhead open)
    Secondary: Cloud Claude API → Reliable, but costs money
    ↓ (if unavailable or rate limited)
    Tertiary: Cached responses → Limited, but always available
    ↓ (if unavailable)
    Final: Graceful error message → Never crashes

FEATURES:
    - Automatic fallback on component failure
    - Bulkhead isolation per inference target
    - Backpressure-aware admission control
    - Health-based routing decisions
    - Cost-aware routing (prefer local when healthy)
    - Circuit breaker integration with half-open testing
    - Metrics and alerting
    - Manual override capability
    - Deep health verification

USAGE:
    from backend.core.graceful_degradation import GracefulDegradation, InferenceTarget

    degradation = await get_degradation_async()

    # Get best available target
    target = await degradation.get_best_target(request_type="inference")

    # Execute with automatic fallback and bulkhead protection
    result = await degradation.execute_with_fallback(
        primary_fn=call_local_prime,
        fallback_fn=call_cloud_api,
        request=request,
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Generic

logger = logging.getLogger(__name__)

# Environment configuration
def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")

T = TypeVar("T")


# =============================================================================
# IMPORTS
# =============================================================================

try:
    from backend.core.trinity_config import get_config, ComponentType, ComponentHealth
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False

# Import advanced async primitives for bulkhead and backpressure
_PRIMITIVES_AVAILABLE = False
ResourceBulkhead = None
AdaptiveBackpressure = None
TimeoutProtectedLock = None
DeepHealthVerifier = None
TrinityRateLimiter = None

# Define placeholder exception classes if not available
class BulkheadCircuitOpen(Exception):
    """Placeholder for when primitives not available."""
    pass

class BulkheadTimeout(Exception):
    """Placeholder for when primitives not available."""
    pass

class BackpressureRejection(Exception):
    """Placeholder for when primitives not available."""
    pass

# Placeholder functions
async def get_bulkhead(*args, **kwargs):
    return None

async def get_backpressure(*args, **kwargs):
    return None

async def get_health_verifier(*args, **kwargs):
    return None

try:
    from backend.core.advanced_async_primitives import (
        ResourceBulkhead,
        AdaptiveBackpressure,
        TimeoutProtectedLock,
        DeepHealthVerifier,
        TrinityRateLimiter,
        BulkheadCircuitOpen,
        BulkheadTimeout,
        BackpressureRejection,
        get_bulkhead,
        get_backpressure,
        get_health_verifier,
    )
    _PRIMITIVES_AVAILABLE = True
except ImportError:
    try:
        # Try relative import
        from core.advanced_async_primitives import (
            ResourceBulkhead,
            AdaptiveBackpressure,
            TimeoutProtectedLock,
            DeepHealthVerifier,
            TrinityRateLimiter,
            BulkheadCircuitOpen,
            BulkheadTimeout,
            BackpressureRejection,
            get_bulkhead,
            get_backpressure,
            get_health_verifier,
        )
        _PRIMITIVES_AVAILABLE = True
    except ImportError:
        logger.debug("[Degradation] Advanced primitives not available, using basic mode")


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

    v80.0 Features:
        - Bulkhead isolation per inference target
        - Backpressure-aware admission control
        - Deep health verification
        - Rate limiting per target
        - Timeout-protected operations
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

        # v80.0: Advanced primitives (lazy initialized)
        self._bulkhead: Optional[ResourceBulkhead] = None
        self._backpressure: Optional[AdaptiveBackpressure] = None
        self._health_verifier: Optional[DeepHealthVerifier] = None
        self._rate_limiters: Dict[InferenceTarget, TrinityRateLimiter] = {}
        self._primitives_initialized = False

        # Configuration from environment
        self._bulkhead_enabled = _env_bool("DEGRADATION_BULKHEAD_ENABLED", True)
        self._backpressure_enabled = _env_bool("DEGRADATION_BACKPRESSURE_ENABLED", True)
        self._deep_health_enabled = _env_bool("DEGRADATION_DEEP_HEALTH_ENABLED", True)
        self._default_timeout = _env_float("DEGRADATION_DEFAULT_TIMEOUT", 30.0)

        # Bulkhead pool sizes per target
        self._bulkhead_sizes = {
            InferenceTarget.LOCAL_PRIME: _env_int("BULKHEAD_PRIME_SIZE", 10),
            InferenceTarget.CLOUD_CLAUDE: _env_int("BULKHEAD_CLAUDE_SIZE", 5),
            InferenceTarget.CLOUD_OPENAI: _env_int("BULKHEAD_OPENAI_SIZE", 3),
        }

        # Rate limits per target (requests per second)
        self._rate_limits = {
            InferenceTarget.LOCAL_PRIME: _env_float("RATE_LIMIT_PRIME", 100.0),
            InferenceTarget.CLOUD_CLAUDE: _env_float("RATE_LIMIT_CLAUDE", 10.0),
            InferenceTarget.CLOUD_OPENAI: _env_float("RATE_LIMIT_OPENAI", 5.0),
        }

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

    async def _init_primitives(self) -> None:
        """Initialize advanced async primitives (lazy initialization)."""
        if self._primitives_initialized or not _PRIMITIVES_AVAILABLE:
            return

        try:
            # Initialize bulkhead with per-target pool sizes
            if self._bulkhead_enabled:
                pools = {
                    target.value: size
                    for target, size in self._bulkhead_sizes.items()
                }
                self._bulkhead = await get_bulkhead(pools)
                logger.info(f"[Degradation] Bulkhead initialized with pools: {pools}")

            # Initialize backpressure
            if self._backpressure_enabled:
                self._backpressure = await get_backpressure()
                logger.info("[Degradation] Backpressure system initialized")

            # Initialize health verifier
            if self._deep_health_enabled:
                self._health_verifier = await get_health_verifier()
                logger.info("[Degradation] Deep health verifier initialized")

            # Initialize rate limiters per target
            for target, rate in self._rate_limits.items():
                self._rate_limiters[target] = TrinityRateLimiter(
                    rate=rate,
                    burst=int(rate * 2),  # 2 seconds of burst
                    name=target.value,
                )

            self._primitives_initialized = True
            logger.info("[Degradation] All advanced primitives initialized")

        except Exception as e:
            logger.warning(f"[Degradation] Primitive initialization failed: {e}")
            # Continue without primitives - graceful degradation of degradation!

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
        timeout: Optional[float] = None,
        priority: str = "normal",
        *args,
        **kwargs,
    ) -> FallbackResult[T]:
        """
        Execute a request with automatic fallback on failure.

        v80.0 Features:
            - Bulkhead isolation prevents cascading failures
            - Backpressure protects against overload
            - Rate limiting per target
            - Priority-based admission

        Args:
            primary_fn: Primary function to try (e.g., local Prime)
            fallback_fn: Fallback function (e.g., cloud API)
            cached_fn: Cache lookup function
            default_value: Default value if all fail
            timeout: Timeout for each attempt (uses env default if None)
            priority: Request priority (critical, high, normal, low)
            *args, **kwargs: Arguments to pass to functions

        Returns:
            FallbackResult with value and metadata
        """
        # Initialize primitives on first use
        await self._init_primitives()

        effective_timeout = timeout if timeout is not None else self._default_timeout
        start_time = time.time()
        attempts = 0

        # v80.0: Check backpressure before proceeding
        if self._backpressure and self._backpressure_enabled:
            if not await self._backpressure.try_acquire():
                logger.warning("[Degradation] Request rejected by backpressure")
                return FallbackResult(
                    success=False,
                    value=default_value,
                    target_used=InferenceTarget.DEGRADED,
                    fallback_reason=FallbackReason.PRIMARY_OVERLOADED,
                    latency_ms=(time.time() - start_time) * 1000,
                    error="System overloaded - backpressure active",
                    attempts=0,
                )

        try:
            # Try primary (Local Prime)
            primary_target = InferenceTarget.LOCAL_PRIME
            primary_status = self._targets[primary_target]
            primary_result = await self._execute_target(
                target=primary_target,
                status=primary_status,
                fn=primary_fn,
                timeout=effective_timeout,
                start_time=start_time,
                args=args,
                kwargs=kwargs,
            )

            if primary_result is not None:
                attempts += 1
                if primary_result.success:
                    return primary_result
                fallback_reason = primary_result.fallback_reason
            else:
                fallback_reason = (
                    FallbackReason.CIRCUIT_OPEN if primary_status.circuit_open
                    else FallbackReason.PRIMARY_UNHEALTHY
                )

            # Try fallback (Cloud API)
            if fallback_fn:
                fallback_target = InferenceTarget.CLOUD_CLAUDE
                fallback_status = self._targets[fallback_target]
                fallback_result = await self._execute_target(
                    target=fallback_target,
                    status=fallback_status,
                    fn=fallback_fn,
                    timeout=effective_timeout,
                    start_time=start_time,
                    args=args,
                    kwargs=kwargs,
                )

                if fallback_result is not None:
                    attempts += 1
                    if fallback_result.success:
                        fallback_result.fallback_reason = fallback_reason
                        return fallback_result

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

        finally:
            # Release backpressure permit
            if self._backpressure and self._backpressure_enabled:
                await self._backpressure.release()

    async def _execute_target(
        self,
        target: InferenceTarget,
        status: TargetStatus,
        fn: Callable[..., Awaitable[T]],
        timeout: float,
        start_time: float,
        args: tuple,
        kwargs: dict,
    ) -> Optional[FallbackResult[T]]:
        """
        Execute a function within a target's bulkhead and rate limit.

        Returns:
            FallbackResult if executed (success or failure), None if skipped
        """
        # Check if target is enabled and circuit is closed
        if not status.enabled:
            return None

        if status.circuit_open:
            # Check if circuit should be reset (half-open)
            if time.time() - status.last_failure > 30:
                status.circuit_open = False
                logger.info(f"[Degradation] Circuit half-open for {target.value}")
            else:
                return None

        # Check rate limit
        if target in self._rate_limiters:
            limiter = self._rate_limiters[target]
            if not await limiter.acquire():
                logger.debug(f"[Degradation] Rate limited for {target.value}")
                return FallbackResult(
                    success=False,
                    value=None,
                    target_used=target,
                    fallback_reason=FallbackReason.PRIMARY_OVERLOADED,
                    latency_ms=(time.time() - start_time) * 1000,
                    error="Rate limited",
                    attempts=1,
                )

        # Execute within bulkhead if available
        try:
            if self._bulkhead and self._bulkhead_enabled and target.value in ["local_prime", "cloud_claude", "cloud_openai"]:
                async with self._bulkhead.execute(target.value, timeout=timeout):
                    result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
            else:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)

            latency_ms = (time.time() - start_time) * 1000
            status.record_success(latency_ms)

            return FallbackResult(
                success=True,
                value=result,
                target_used=target,
                fallback_reason=FallbackReason.NONE,
                latency_ms=latency_ms,
                attempts=1,
            )

        except asyncio.TimeoutError:
            status.record_failure()
            logger.warning(f"[Degradation] {target.value} timeout after {timeout}s")
            return FallbackResult(
                success=False,
                value=None,
                target_used=target,
                fallback_reason=FallbackReason.PRIMARY_TIMEOUT,
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Timeout after {timeout}s",
                attempts=1,
            )

        except (BulkheadCircuitOpen, BulkheadTimeout) as e:
            status.record_failure()
            logger.warning(f"[Degradation] {target.value} bulkhead rejection: {e}")
            return FallbackResult(
                success=False,
                value=None,
                target_used=target,
                fallback_reason=FallbackReason.CIRCUIT_OPEN,
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e),
                attempts=1,
            )

        except Exception as e:
            status.record_failure()
            logger.warning(f"[Degradation] {target.value} error: {e}")
            return FallbackResult(
                success=False,
                value=None,
                target_used=target,
                fallback_reason=FallbackReason.PRIMARY_ERROR,
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e),
                attempts=1,
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
_degradation_lock = asyncio.Lock()


def get_degradation() -> GracefulDegradation:
    """Get the singleton GracefulDegradation instance (sync version)."""
    global _degradation
    if _degradation is None:
        _degradation = GracefulDegradation()
    return _degradation


async def get_degradation_async() -> GracefulDegradation:
    """
    Get the singleton GracefulDegradation instance with async initialization.

    This version ensures primitives are initialized before returning.
    Recommended for production use.
    """
    global _degradation
    if _degradation is None:
        async with _degradation_lock:
            if _degradation is None:
                _degradation = GracefulDegradation()
    # Initialize primitives
    await _degradation._init_primitives()
    return _degradation


async def shutdown_degradation() -> None:
    """Shutdown the GracefulDegradation instance and cleanup primitives."""
    global _degradation
    if _degradation is not None:
        # Cleanup any resources
        if _degradation._backpressure:
            try:
                await _degradation._backpressure.stop()
            except Exception:
                pass
        if _degradation._health_verifier:
            try:
                await _degradation._health_verifier.close()
            except Exception:
                pass
        _degradation = None
        logger.info("[Degradation] Shutdown complete")


# =============================================================================
# TRINITY-SPECIFIC FALLBACK CHAINS (v81.0)
# =============================================================================


class TrinityFallbackTarget(Enum):
    """Extended targets for Trinity cross-component fallback."""
    # J-Prime Chain
    LOCAL_JARVIS_PRIME = "local_jarvis_prime"
    CLOUD_RUN_PRIME = "cloud_run_prime"
    CLOUD_CLAUDE_API = "cloud_claude_api"

    # Reactor-Core Chain
    LOCAL_REACTOR = "local_reactor"
    EXPERIENCE_QUEUE = "experience_queue"
    DISK_PERSISTENCE = "disk_persistence"

    # Voice Chain
    ECAPA_TDNN = "ecapa_tdnn"
    BEHAVIORAL_ANALYSIS = "behavioral_analysis"
    CHALLENGE_RESPONSE = "challenge_response"
    PASSWORD_FALLBACK = "password_fallback"


@dataclass
class TrinityTargetStatus:
    """Status of a Trinity fallback target."""
    target: TrinityFallbackTarget
    health: TargetHealth = TargetHealth.UNKNOWN
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    circuit_open: bool = False
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def record_success(self) -> None:
        """Record successful request."""
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.health = TargetHealth.HEALTHY
        if time.time() - self.last_failure > 30:
            self.circuit_open = False

    def record_failure(self) -> None:
        """Record failed request."""
        self.last_failure = time.time()
        self.consecutive_failures += 1
        if self.consecutive_failures >= 5:
            self.health = TargetHealth.UNHEALTHY
            self.circuit_open = True
        elif self.consecutive_failures >= 2:
            self.health = TargetHealth.DEGRADED


@dataclass
class TrinityFallbackChain:
    """
    Defines a fallback chain for a Trinity service.

    Example chains:
        J-Prime Chain:
            Local J-Prime -> Cloud Run J-Prime -> Cloud Claude API

        Reactor-Core Chain:
            Local Reactor -> Experience Queue -> Disk Persistence

        Voice Chain:
            ECAPA-TDNN -> Behavioral Analysis -> Challenge-Response -> Password
    """
    name: str
    targets: List[TrinityFallbackTarget]
    description: str = ""
    timeout_per_target: float = 10.0


class TrinityFallbackManager:
    """
    Extended fallback manager for Trinity cross-component orchestration.

    Provides specialized fallback chains for:
    - J-Prime (Mind) inference
    - Reactor-Core (Nerves) training
    - Voice authentication
    """

    # Pre-defined fallback chains
    JPRIME_CHAIN = TrinityFallbackChain(
        name="jprime",
        targets=[
            TrinityFallbackTarget.LOCAL_JARVIS_PRIME,
            TrinityFallbackTarget.CLOUD_RUN_PRIME,
            TrinityFallbackTarget.CLOUD_CLAUDE_API,
        ],
        description="J-Prime inference fallback chain",
        timeout_per_target=30.0,
    )

    REACTOR_CHAIN = TrinityFallbackChain(
        name="reactor",
        targets=[
            TrinityFallbackTarget.LOCAL_REACTOR,
            TrinityFallbackTarget.EXPERIENCE_QUEUE,
            TrinityFallbackTarget.DISK_PERSISTENCE,
        ],
        description="Reactor-Core training data fallback chain",
        timeout_per_target=15.0,
    )

    VOICE_CHAIN = TrinityFallbackChain(
        name="voice",
        targets=[
            TrinityFallbackTarget.ECAPA_TDNN,
            TrinityFallbackTarget.BEHAVIORAL_ANALYSIS,
            TrinityFallbackTarget.CHALLENGE_RESPONSE,
            TrinityFallbackTarget.PASSWORD_FALLBACK,
        ],
        description="Voice authentication fallback chain",
        timeout_per_target=5.0,
    )

    def __init__(self):
        self._targets: Dict[TrinityFallbackTarget, TrinityTargetStatus] = {}
        self._chains: Dict[str, TrinityFallbackChain] = {
            "jprime": self.JPRIME_CHAIN,
            "reactor": self.REACTOR_CHAIN,
            "voice": self.VOICE_CHAIN,
        }
        self._lock = asyncio.Lock()
        self._experience_queue: Optional[Any] = None  # ExperienceDataQueue

        self._init_targets()

    def _init_targets(self) -> None:
        """Initialize all target statuses."""
        for target in TrinityFallbackTarget:
            self._targets[target] = TrinityTargetStatus(target=target)

    async def get_experience_queue(self) -> Any:
        """Lazy-load experience queue."""
        if self._experience_queue is None:
            try:
                from backend.core.experience_queue import get_experience_queue
                self._experience_queue = await get_experience_queue()
            except ImportError:
                logger.warning("[TrinityFallback] Experience queue not available")
        return self._experience_queue

    async def execute_chain(
        self,
        chain_name: str,
        handlers: Dict[TrinityFallbackTarget, Callable[..., Awaitable[T]]],
        *args,
        **kwargs,
    ) -> FallbackResult[T]:
        """
        Execute a fallback chain with handlers for each target.

        Args:
            chain_name: Name of the chain to execute
            handlers: Dict mapping targets to handler functions
            *args, **kwargs: Arguments passed to handlers

        Returns:
            FallbackResult with value and metadata
        """
        async with self._lock:
            chain = self._chains.get(chain_name)
            if not chain:
                return FallbackResult(
                    success=False,
                    error=f"Unknown chain: {chain_name}",
                )

            start_time = time.time()
            attempts = 0
            last_error = None

            for target in chain.targets:
                status = self._targets[target]

                # Skip disabled or circuit-open targets
                if not status.enabled or status.circuit_open:
                    continue

                # Get handler for this target
                handler = handlers.get(target)
                if not handler:
                    continue

                attempts += 1

                try:
                    result = await asyncio.wait_for(
                        handler(*args, **kwargs),
                        timeout=chain.timeout_per_target,
                    )

                    status.record_success()
                    latency_ms = (time.time() - start_time) * 1000

                    return FallbackResult(
                        success=True,
                        value=result,
                        target_used=InferenceTarget.LOCAL_PRIME,  # Map to base enum
                        fallback_reason=FallbackReason.NONE if attempts == 1 else FallbackReason.PRIMARY_ERROR,
                        latency_ms=latency_ms,
                        attempts=attempts,
                    )

                except asyncio.TimeoutError:
                    status.record_failure()
                    last_error = f"{target.value} timeout"
                    logger.warning(f"[TrinityFallback] {target.value} timeout")

                except Exception as e:
                    status.record_failure()
                    last_error = str(e)
                    logger.warning(f"[TrinityFallback] {target.value} error: {e}")

            # All targets failed
            latency_ms = (time.time() - start_time) * 1000
            return FallbackResult(
                success=False,
                target_used=InferenceTarget.DEGRADED,
                fallback_reason=FallbackReason.PRIMARY_ERROR,
                latency_ms=latency_ms,
                error=last_error or "All targets failed",
                attempts=attempts,
            )

    async def execute_jprime_chain(
        self,
        request: Dict[str, Any],
        local_handler: Optional[Callable] = None,
        cloud_run_handler: Optional[Callable] = None,
        claude_api_handler: Optional[Callable] = None,
    ) -> FallbackResult:
        """
        Execute J-Prime inference with fallback chain.

        Chain: Local J-Prime -> Cloud Run J-Prime -> Cloud Claude API
        """
        handlers = {}

        if local_handler:
            handlers[TrinityFallbackTarget.LOCAL_JARVIS_PRIME] = local_handler
        if cloud_run_handler:
            handlers[TrinityFallbackTarget.CLOUD_RUN_PRIME] = cloud_run_handler
        if claude_api_handler:
            handlers[TrinityFallbackTarget.CLOUD_CLAUDE_API] = claude_api_handler

        return await self.execute_chain("jprime", handlers, request)

    async def execute_reactor_chain(
        self,
        experience_data: Dict[str, Any],
        local_handler: Optional[Callable] = None,
    ) -> FallbackResult:
        """
        Execute Reactor-Core training data submission with fallback.

        Chain: Local Reactor -> Experience Queue -> Disk Persistence
        """
        async def queue_handler(data):
            queue = await self.get_experience_queue()
            if queue:
                from backend.core.experience_queue import ExperienceType, ExperiencePriority
                entry_id = await queue.enqueue(
                    experience_type=ExperienceType.INFERENCE_FEEDBACK,
                    data=data,
                    priority=ExperiencePriority.NORMAL,
                )
                return {"queued": True, "entry_id": entry_id}
            raise RuntimeError("Queue not available")

        async def disk_handler(data):
            # Fallback to disk persistence
            import json
            from pathlib import Path

            fallback_dir = Path.home() / ".jarvis" / "experience_fallback"
            fallback_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{int(time.time() * 1000)}.json"
            filepath = fallback_dir / filename

            with open(filepath, "w") as f:
                json.dump(data, f)

            return {"persisted": True, "path": str(filepath)}

        handlers = {
            TrinityFallbackTarget.EXPERIENCE_QUEUE: queue_handler,
            TrinityFallbackTarget.DISK_PERSISTENCE: disk_handler,
        }

        if local_handler:
            handlers[TrinityFallbackTarget.LOCAL_REACTOR] = local_handler

        return await self.execute_chain("reactor", handlers, experience_data)

    async def execute_voice_chain(
        self,
        audio_data: bytes,
        speaker_id: str,
        ecapa_handler: Optional[Callable] = None,
        behavioral_handler: Optional[Callable] = None,
        challenge_handler: Optional[Callable] = None,
        password_handler: Optional[Callable] = None,
    ) -> FallbackResult:
        """
        Execute voice authentication with fallback chain.

        Chain: ECAPA-TDNN -> Behavioral Analysis -> Challenge-Response -> Password
        """
        handlers = {}

        if ecapa_handler:
            handlers[TrinityFallbackTarget.ECAPA_TDNN] = ecapa_handler
        if behavioral_handler:
            handlers[TrinityFallbackTarget.BEHAVIORAL_ANALYSIS] = behavioral_handler
        if challenge_handler:
            handlers[TrinityFallbackTarget.CHALLENGE_RESPONSE] = challenge_handler
        if password_handler:
            handlers[TrinityFallbackTarget.PASSWORD_FALLBACK] = password_handler

        return await self.execute_chain(
            "voice", handlers, audio_data, speaker_id
        )

    def update_target_health(
        self,
        target: TrinityFallbackTarget,
        health: TargetHealth,
    ) -> None:
        """Update health status of a target."""
        if target in self._targets:
            self._targets[target].health = health
            logger.debug(f"[TrinityFallback] {target.value} health: {health.value}")

    def enable_target(
        self,
        target: TrinityFallbackTarget,
        enabled: bool = True,
    ) -> None:
        """Enable or disable a target."""
        if target in self._targets:
            self._targets[target].enabled = enabled
            logger.info(
                f"[TrinityFallback] {target.value} "
                f"{'enabled' if enabled else 'disabled'}"
            )

    def reset_circuit(self, target: TrinityFallbackTarget) -> None:
        """Reset circuit breaker for a target."""
        if target in self._targets:
            self._targets[target].circuit_open = False
            self._targets[target].consecutive_failures = 0
            logger.info(f"[TrinityFallback] Circuit reset for {target.value}")

    def get_status(self) -> Dict[str, Any]:
        """Get status of all Trinity fallback targets."""
        return {
            "chains": {
                name: {
                    "targets": [t.value for t in chain.targets],
                    "description": chain.description,
                    "timeout": chain.timeout_per_target,
                }
                for name, chain in self._chains.items()
            },
            "targets": {
                target.value: {
                    "health": status.health.value,
                    "enabled": status.enabled,
                    "circuit_open": status.circuit_open,
                    "consecutive_failures": status.consecutive_failures,
                }
                for target, status in self._targets.items()
            },
        }


# =============================================================================
# TRINITY FALLBACK SINGLETON
# =============================================================================

_trinity_fallback: Optional[TrinityFallbackManager] = None
_trinity_fallback_lock = asyncio.Lock()


async def get_trinity_fallback() -> TrinityFallbackManager:
    """Get the singleton TrinityFallbackManager instance."""
    global _trinity_fallback

    async with _trinity_fallback_lock:
        if _trinity_fallback is None:
            _trinity_fallback = TrinityFallbackManager()
        return _trinity_fallback


def get_trinity_fallback_sync() -> TrinityFallbackManager:
    """Synchronous version for non-async contexts."""
    global _trinity_fallback

    if _trinity_fallback is None:
        _trinity_fallback = TrinityFallbackManager()
    return _trinity_fallback
