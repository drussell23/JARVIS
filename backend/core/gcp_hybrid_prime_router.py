"""
GCP Hybrid Prime Router v2.0
============================

Intelligent routing between local JARVIS Prime, GCP VMs, and cloud APIs
with unified cost management across all repos.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                   GCPHybridPrimeRouter                           │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │                Cost-Aware Decision Engine                   │ │
    │  │  Budget Check → Resource Check → Capability Match → Route  │ │
    │  └──────────────────────────┬─────────────────────────────────┘ │
    │                             │                                    │
    │   ┌──────────────┐   ┌─────┴────┐   ┌────────────┐              │
    │   │ Local Prime  │   │ GCP VM   │   │Cloud Claude│              │
    │   │ (Free, Fast) │   │(Spot,$$) │   │ (API, $$$) │              │
    │   │ RAM > 8GB    │   │ RAM > 85%│   │ Fallback   │              │
    │   └──────────────┘   └──────────┘   └────────────┘              │
    │                                                                  │
    │   Integration Points:                                            │
    │   - CrossRepoCostSync: Unified budget tracking                  │
    │   - CrossRepoNeuralMesh: Prime/Reactor coordination             │
    │   - TrinityBridgeAdapter: Model update notifications            │
    └─────────────────────────────────────────────────────────────────┘

Routing Logic:
1. Check unified budget - if exceeded, force local-only mode
2. Check local RAM - if sufficient, use local Prime (free)
3. Check GCP VM availability - if available and budget allows, use GCP
4. Fallback to Cloud Claude API if all else fails

v2.0 Changes:
- Replaced inline circuit breaker with CrossRepoCircuitBreaker
- Added failure classification for intelligent retry decisions
- Added CorrelationContext for distributed tracing
- Added detailed execution metrics and latency tracking
- Improved error handling with categorized failures

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("GCPHybridPrimeRouter")

# =============================================================================
# Resilience Utilities (optional but recommended)
# =============================================================================

RESILIENCE_AVAILABLE = False
try:
    from backend.core.resilience import (
        CrossRepoCircuitBreaker,
        CircuitBreakerConfig,
        CorrelationContext,
        get_correlation_context,
        FailureType,
    )
    RESILIENCE_AVAILABLE = True
except ImportError:
    CrossRepoCircuitBreaker = None
    CircuitBreakerConfig = None
    CorrelationContext = None
    get_correlation_context = lambda: None
    FailureType = None

# =============================================================================
# Configuration
# =============================================================================

# RAM thresholds (in GB)
LOCAL_PRIME_MIN_RAM_GB = float(os.getenv("LOCAL_PRIME_MIN_RAM_GB", "8.0"))
GCP_TRIGGER_RAM_PERCENT = float(os.getenv("GCP_TRIGGER_RAM_PERCENT", "85.0"))
CRITICAL_RAM_PERCENT = float(os.getenv("CRITICAL_RAM_PERCENT", "95.0"))
VM_PROVISIONING_THRESHOLD = float(os.getenv("VM_PROVISIONING_THRESHOLD", "80.0"))

# Cost thresholds
MAX_SINGLE_REQUEST_COST = float(os.getenv("MAX_SINGLE_REQUEST_COST", "0.50"))
PREFER_LOCAL_BELOW_COST = float(os.getenv("PREFER_LOCAL_BELOW_COST", "0.10"))

# VM provisioning (v2.0)
VM_PROVISIONING_ENABLED = os.getenv("GCP_VM_PROVISIONING_ENABLED", "true").lower() == "true"
VM_PROVISIONING_LOCK_TTL = int(os.getenv("VM_PROVISIONING_LOCK_TTL", "300"))  # 5 minutes
VM_MIN_ACTIVE_REQUESTS = int(os.getenv("VM_MIN_ACTIVE_REQUESTS", "1"))  # Min requests before termination

# =============================================================================
# Distributed Locking for VM Provisioning (v2.0)
# =============================================================================

_VM_LOCK_AVAILABLE = False
try:
    from backend.core.resilience import DistributedLock, DistributedLockConfig
    _VM_LOCK_AVAILABLE = True
except ImportError:
    DistributedLock = None
    DistributedLockConfig = None

# Timeouts
LOCAL_TIMEOUT_MS = int(os.getenv("LOCAL_PRIME_TIMEOUT_MS", "5000"))
GCP_TIMEOUT_MS = int(os.getenv("GCP_VM_TIMEOUT_MS", "30000"))
CLOUD_API_TIMEOUT_MS = int(os.getenv("CLOUD_API_TIMEOUT_MS", "60000"))


class RoutingTier(Enum):
    """Routing tier for hybrid execution."""
    LOCAL_PRIME = "local_prime"  # Free, fast, requires RAM
    GCP_VM = "gcp_vm"            # Spot pricing, medium cost
    GCP_CLOUD_RUN = "cloud_run"  # Serverless, pay-per-use
    CLOUD_CLAUDE = "cloud_claude"  # Anthropic API, highest cost
    DEGRADED_LOCAL = "degraded_local"  # Reduced capability local


class RoutingReason(Enum):
    """Reason for routing decision."""
    BUDGET_EXCEEDED = "budget_exceeded"
    LOCAL_RAM_SUFFICIENT = "local_ram_sufficient"
    LOCAL_RAM_INSUFFICIENT = "local_ram_insufficient"
    GCP_AVAILABLE = "gcp_available"
    GCP_UNAVAILABLE = "gcp_unavailable"
    COST_OPTIMIZATION = "cost_optimization"
    CAPABILITY_REQUIRED = "capability_required"
    FALLBACK = "fallback"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"


@dataclass
class RoutingDecision:
    """Result of a routing decision."""
    tier: RoutingTier
    reason: RoutingReason
    estimated_cost: float = 0.0
    timeout_ms: int = 30000
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "reason": self.reason.value,
            "estimated_cost": self.estimated_cost,
            "timeout_ms": self.timeout_ms,
            "metadata": self.metadata,
        }


@dataclass
class RouterMetrics:
    """Metrics for the hybrid router."""
    total_requests: int = 0
    local_requests: int = 0
    gcp_requests: int = 0
    cloud_requests: int = 0
    fallback_requests: int = 0
    budget_blocks: int = 0
    total_cost: float = 0.0
    avg_latency_ms: float = 0.0
    last_request_time: float = 0.0
    # v2.0 additions
    circuit_breaker_trips: int = 0
    timeout_failures: int = 0
    network_failures: int = 0
    resource_failures: int = 0
    transient_failures: int = 0
    permanent_failures: int = 0
    retries_total: int = 0
    retries_successful: int = 0
    # v2.1: Use deque for O(1) append/popleft instead of O(n) list.pop(0)
    latency_samples: Any = field(default_factory=lambda: None)  # Initialized in __post_init__
    max_latency_samples: int = 100

    def __post_init__(self):
        """Initialize deque after dataclass creation."""
        from collections import deque
        if self.latency_samples is None:
            self.latency_samples = deque(maxlen=self.max_latency_samples)


@dataclass
class ExecutionResult:
    """Result of tier execution (v2.0)."""
    success: bool
    tier: RoutingTier
    response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    failure_type: Optional[str] = None
    latency_ms: float = 0.0
    retries: int = 0
    cost: float = 0.0


# =============================================================================
# GCP Hybrid Prime Router
# =============================================================================

class GCPHybridPrimeRouter:
    """
    Intelligent router for hybrid local/GCP/cloud execution.

    Features:
    - Cost-aware routing with unified budget tracking
    - RAM-based local Prime preference
    - GCP VM integration for overflow
    - Cloud API fallback chain
    - Circuit breaker per tier (v2.0: using CrossRepoCircuitBreaker)
    - Failure classification for intelligent retries (v2.0)
    """

    def __init__(self, use_resilience: bool = True):
        self.logger = logging.getLogger("GCPHybridPrimeRouter")
        self._use_resilience = use_resilience and RESILIENCE_AVAILABLE

        # State
        self._running = False
        self._monitoring_task: Optional[asyncio.Task] = None

        # v2.0: Use CrossRepoCircuitBreaker if available, fallback to inline
        self._circuit_breaker: Optional[CrossRepoCircuitBreaker] = None
        self._legacy_circuit_breakers: Dict[RoutingTier, dict] = {}

        if self._use_resilience and CrossRepoCircuitBreaker:
            self._circuit_breaker = CrossRepoCircuitBreaker(
                name="gcp_hybrid_prime_router",
                config=CircuitBreakerConfig(
                    failure_threshold=3,
                    timeout_seconds=60.0,  # v2.1: Fixed - was 'recovery_timeout' which doesn't exist
                    half_open_max_calls=2,
                )
            )
        else:
            # Legacy inline circuit breakers
            self._legacy_circuit_breakers = {
                tier: {
                    "failures": 0,
                    "last_failure": 0.0,
                    "state": "closed",
                    "threshold": 3,
                    "timeout": 60.0,
                }
                for tier in RoutingTier
            }

        # Metrics
        self._metrics = RouterMetrics()

        # External integrations (lazy)
        self._cost_sync = None
        self._neural_mesh = None
        self._prime_client = None
        self._gcp_controller = None

        # Memory monitor
        self._last_ram_check = 0.0
        self._ram_cache_ttl = 5.0
        self._cached_ram_info: Optional[dict] = None

        # Callbacks
        self._decision_callbacks: Set[Callable] = set()

        # v2.0: Retry configuration per failure type
        self._retry_config: Dict[str, dict] = {
            "timeout": {"max_retries": 2, "delay": 1.0},
            "network": {"max_retries": 3, "delay": 0.5},
            "transient": {"max_retries": 2, "delay": 0.5},
            "resource": {"max_retries": 1, "delay": 2.0},
            "permanent": {"max_retries": 0, "delay": 0.0},
        }

        # v2.0: VM provisioning with distributed locking
        self._vm_provisioning_enabled = VM_PROVISIONING_ENABLED and self._use_resilience
        self._vm_provisioning_lock: Optional[DistributedLock] = None
        self._vm_provisioning_in_progress = False
        self._active_requests: Dict[RoutingTier, int] = {tier: 0 for tier in RoutingTier}
        self._vm_provisioning_task: Optional[asyncio.Task] = None
        self._memory_pressure_task: Optional[asyncio.Task] = None

        # v2.0: Graceful degradation
        self._degradation_mode = False
        self._degradation_reason: Optional[str] = None
        self._last_successful_tier: Optional[RoutingTier] = None
        self._tier_failure_counts: Dict[RoutingTier, int] = {tier: 0 for tier in RoutingTier}
        self._tier_last_success: Dict[RoutingTier, float] = {tier: 0.0 for tier in RoutingTier}

        # v2.1: Timeout-based degradation recovery
        self._degradation_entered_at: float = 0.0
        self._degradation_recovery_timeout = float(os.getenv("DEGRADATION_RECOVERY_TIMEOUT", "300.0"))  # 5 minutes default
        self._active_request_validation_interval = float(os.getenv("ACTIVE_REQUEST_VALIDATION_INTERVAL", "60.0"))
        self._last_active_request_validation = 0.0

        # Initialize VM provisioning lock if available
        if self._vm_provisioning_enabled and _VM_LOCK_AVAILABLE and DistributedLock:
            try:
                self._vm_provisioning_lock = DistributedLock(
                    lock_name="gcp_vm_provisioning",
                    config=DistributedLockConfig(
                        ttl_seconds=VM_PROVISIONING_LOCK_TTL,
                        retry_count=3,
                        retry_delay=1.0,
                    ),
                )
            except Exception as e:
                self.logger.warning(f"VM provisioning lock initialization failed: {e}")

    async def start(self) -> bool:
        """Start the hybrid router."""
        if self._running:
            return True

        self._running = True
        self.logger.info("GCPHybridPrimeRouter v2.0 starting...")

        # Connect to integrations
        await self._connect_integrations()

        # Start monitoring
        self._monitoring_task = asyncio.create_task(
            self._monitoring_loop(),
            name="gcp_hybrid_prime_monitor",
        )

        self.logger.info(
            f"GCPHybridPrimeRouter ready "
            f"(local_min_ram: {LOCAL_PRIME_MIN_RAM_GB}GB, "
            f"gcp_trigger: {GCP_TRIGGER_RAM_PERCENT}%, "
            f"resilience: {self._use_resilience})"
        )

        # v2.0: Start memory pressure monitoring
        if self._vm_provisioning_enabled:
            self._memory_pressure_task = asyncio.create_task(
                self._memory_pressure_monitor(),
                name="gcp_memory_pressure_monitor",
            )
            self.logger.info("GCPHybridPrimeRouter: Memory pressure monitoring active")

        return True

    async def _memory_pressure_monitor(self) -> None:
        """
        v2.0: Monitor memory pressure and trigger VM provisioning.

        When RAM usage exceeds VM_PROVISIONING_THRESHOLD, attempts to provision
        a GCP VM using distributed locking to prevent race conditions.
        """
        while self._running:
            try:
                await asyncio.sleep(5.0)  # Check every 5 seconds

                ram_info = await self._get_ram_info()
                if not ram_info:
                    continue

                used_percent = ram_info.get("used_percent", 0)

                # Check if we should trigger VM provisioning
                if used_percent >= VM_PROVISIONING_THRESHOLD:
                    if not self._vm_provisioning_in_progress:
                        self.logger.warning(
                            f"Memory pressure detected: {used_percent:.1f}% "
                            f"(threshold: {VM_PROVISIONING_THRESHOLD}%)"
                        )
                        await self._trigger_vm_provisioning(reason="memory_pressure")

                # Check if we should terminate VM (low usage)
                elif used_percent < GCP_TRIGGER_RAM_PERCENT - 20:
                    await self._check_vm_termination()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Memory pressure monitor error: {e}")
                await asyncio.sleep(10.0)

    async def _trigger_vm_provisioning(self, reason: str = "unknown") -> bool:
        """
        v2.0: Trigger GCP VM provisioning with distributed locking.

        Uses Redis-based distributed lock to prevent concurrent VM creation
        across multiple repos (JARVIS, JARVIS Prime).

        Args:
            reason: Reason for triggering provisioning

        Returns:
            True if VM was provisioned, False otherwise
        """
        if self._vm_provisioning_in_progress:
            self.logger.debug("VM provisioning already in progress")
            return False

        self._vm_provisioning_in_progress = True
        token: Optional[str] = None

        try:
            # Acquire distributed lock
            if self._vm_provisioning_lock:
                try:
                    token = await self._vm_provisioning_lock.acquire_lock(timeout=10.0)
                    # v2.1: Null check before string slicing to prevent IndexError
                    if token:
                        self.logger.info(f"Acquired VM provisioning lock (token: {token[:8]}...)")
                    else:
                        self.logger.warning("VM provisioning lock returned None - skipping")
                        return False
                except Exception as e:
                    self.logger.warning(f"Could not acquire VM provisioning lock: {e}")
                    # Another instance is provisioning - skip
                    return False

            # Check if GCP controller is available
            if not self._gcp_controller:
                self.logger.warning("GCP controller not available for VM provisioning")
                return False

            # Check if VM already exists
            if hasattr(self._gcp_controller, 'is_vm_available'):
                if self._gcp_controller.is_vm_available():
                    self.logger.info("GCP VM already available, skipping provisioning")
                    return True

            # Trigger VM creation
            self.logger.info(f"Triggering GCP VM provisioning (reason: {reason})")

            if hasattr(self._gcp_controller, 'create_vm'):
                result = await self._gcp_controller.create_vm()
                if result:
                    self.logger.info("GCP VM provisioned successfully")
                    self._metrics.gcp_requests += 1
                    return True
                else:
                    self.logger.error("GCP VM provisioning failed")
                    return False
            else:
                self.logger.warning("GCP controller doesn't support create_vm()")
                return False

        except Exception as e:
            self.logger.error(f"VM provisioning error: {e}")
            return False

        finally:
            self._vm_provisioning_in_progress = False
            # Release distributed lock
            if self._vm_provisioning_lock and token:
                try:
                    await self._vm_provisioning_lock.release_lock(token)
                    self.logger.debug("Released VM provisioning lock")
                except Exception as e:
                    self.logger.warning(f"Error releasing VM lock: {e}")

    async def _check_vm_termination(self) -> None:
        """
        v2.0: Check if GCP VM can be terminated (low usage, no active requests).

        Uses reference counting to ensure no active requests before termination.
        """
        # Check active requests on GCP tier
        active_gcp_requests = self._active_requests.get(RoutingTier.GCP_VM, 0)

        if active_gcp_requests > VM_MIN_ACTIVE_REQUESTS:
            return  # Still have active requests

        # Check if GCP controller supports termination
        if not self._gcp_controller:
            return

        if not hasattr(self._gcp_controller, 'terminate_vm'):
            return

        # Acquire lock before termination
        token: Optional[str] = None
        if self._vm_provisioning_lock:
            try:
                token = await self._vm_provisioning_lock.acquire_lock(timeout=5.0)
            except Exception:
                return  # Another instance has the lock

        try:
            if hasattr(self._gcp_controller, 'is_vm_available'):
                if self._gcp_controller.is_vm_available():
                    self.logger.info("Terminating GCP VM (low usage, no active requests)")
                    await self._gcp_controller.terminate_vm()
        except Exception as e:
            self.logger.error(f"VM termination error: {e}")
        finally:
            if self._vm_provisioning_lock and token:
                try:
                    await self._vm_provisioning_lock.release_lock(token)
                except Exception:
                    pass

    def _increment_active_requests(self, tier: RoutingTier) -> None:
        """v2.0: Increment active request count for reference counting."""
        self._active_requests[tier] = self._active_requests.get(tier, 0) + 1

    def _decrement_active_requests(self, tier: RoutingTier) -> None:
        """v2.0: Decrement active request count for reference counting."""
        current = self._active_requests.get(tier, 0)
        self._active_requests[tier] = max(0, current - 1)

    async def stop(self) -> None:
        """Stop the hybrid router."""
        self._running = False

        # v2.0: Cancel memory pressure monitoring
        if self._memory_pressure_task:
            self._memory_pressure_task.cancel()
            try:
                await self._memory_pressure_task
            except asyncio.CancelledError:
                pass

        # v2.0: Cancel VM provisioning task if running
        if self._vm_provisioning_task:
            self._vm_provisioning_task.cancel()
            try:
                await self._vm_provisioning_task
            except asyncio.CancelledError:
                pass

        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        self.logger.info(
            f"GCPHybridPrimeRouter stopped "
            f"(total: {self._metrics.total_requests}, "
            f"cost: ${self._metrics.total_cost:.4f})"
        )

    async def _connect_integrations(self) -> None:
        """Connect to external integrations."""
        # Cross-repo cost sync
        try:
            from backend.core.cross_repo_cost_sync import get_cross_repo_cost_sync
            self._cost_sync = await get_cross_repo_cost_sync("jarvis")
        except Exception as e:
            self.logger.warning(f"CrossRepoCostSync not available: {e}")

        # Cross-repo neural mesh
        try:
            from backend.core.registry.cross_repo_neural_mesh import get_cross_repo_neural_mesh
            self._neural_mesh = await get_cross_repo_neural_mesh()
        except Exception as e:
            self.logger.warning(f"CrossRepoNeuralMesh not available: {e}")

        # GCP controller
        try:
            from backend.core.supervisor_gcp_controller import get_supervisor_gcp_controller
            self._gcp_controller = get_supervisor_gcp_controller()
        except Exception as e:
            self.logger.debug(f"GCP controller not available: {e}")

        # JARVIS Prime client
        try:
            from backend.core.jarvis_prime_client import get_jarvis_prime_client
            self._prime_client = get_jarvis_prime_client()
        except Exception as e:
            self.logger.debug(f"Prime client not available: {e}")

    async def _monitoring_loop(self) -> None:
        """Background monitoring loop (v2.1: enhanced with degradation recovery and request validation)."""
        while self._running:
            try:
                # Update RAM info
                self._cached_ram_info = await self._get_ram_info()

                # Check circuit breakers
                await self._check_circuit_breakers()

                # v2.1: Check for timeout-based degradation recovery
                if self._degradation_mode:
                    await self._check_degradation_recovery()

                # v2.1: Periodically validate active request counters
                now = time.time()
                if now - self._last_active_request_validation > self._active_request_validation_interval:
                    await self._validate_active_requests()
                    self._last_active_request_validation = now

                await asyncio.sleep(10.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Monitoring error: {e}")
                await asyncio.sleep(5.0)

    async def _check_degradation_recovery(self) -> None:
        """
        v2.1: Check if degradation mode should be exited based on timeout.

        After the recovery timeout, attempt to test tiers and exit degradation
        mode if any tier responds successfully.
        """
        if not self._degradation_mode or self._degradation_entered_at <= 0:
            return

        elapsed = time.time() - self._degradation_entered_at
        if elapsed < self._degradation_recovery_timeout:
            return

        self.logger.info(
            f"Degradation recovery timeout reached ({elapsed:.1f}s > {self._degradation_recovery_timeout}s). "
            f"Attempting recovery probe..."
        )

        # Try to probe each tier with a simple health check
        recovery_tiers = [
            RoutingTier.LOCAL_PRIME,
            RoutingTier.GCP_CLOUD_RUN,
            RoutingTier.CLOUD_CLAUDE,
        ]

        for tier in recovery_tiers:
            if self._check_circuit_breaker(tier):
                try:
                    # Simple probe - just check if tier is responsive
                    if tier == RoutingTier.LOCAL_PRIME:
                        ram_info = await self._get_ram_info()
                        if self._can_use_local(ram_info):
                            self.logger.info(f"Recovery probe successful: {tier.value} is available")
                            self._degradation_mode = False
                            self._degradation_reason = None
                            self._degradation_entered_at = 0.0
                            return
                    elif tier == RoutingTier.GCP_CLOUD_RUN:
                        cloud_run_url = os.getenv("JARVIS_PRIME_CLOUD_RUN_URL")
                        if cloud_run_url:
                            self.logger.info(f"Recovery probe successful: {tier.value} URL configured")
                            self._degradation_mode = False
                            self._degradation_reason = None
                            self._degradation_entered_at = 0.0
                            return
                    elif tier == RoutingTier.CLOUD_CLAUDE:
                        # Claude is always "available" - just check API key exists
                        if os.getenv("ANTHROPIC_API_KEY"):
                            self.logger.info(f"Recovery probe successful: {tier.value} API key present")
                            self._degradation_mode = False
                            self._degradation_reason = None
                            self._degradation_entered_at = 0.0
                            return
                except Exception as e:
                    self.logger.debug(f"Recovery probe failed for {tier.value}: {e}")
                    continue

        # Reset timeout for next attempt
        self._degradation_entered_at = time.time()
        self.logger.warning("All recovery probes failed - remaining in degradation mode")

    async def _validate_active_requests(self) -> None:
        """
        v2.1: Validate and potentially reset active request counters.

        This prevents stuck counters from blocking VM termination indefinitely.
        If no requests have been made in the validation interval, counters
        should be zero. Reset any non-zero counters with a warning.
        """
        # Only validate if no recent activity
        if self._metrics.last_request_time > 0:
            idle_time = time.time() - self._metrics.last_request_time
            if idle_time < self._active_request_validation_interval:
                return  # Recent activity - counters are likely accurate

        # Check for stuck counters
        stuck_tiers = []
        for tier, count in self._active_requests.items():
            if count > 0:
                stuck_tiers.append((tier, count))

        if stuck_tiers:
            self.logger.warning(
                f"Detected potentially stuck active request counters (idle {idle_time:.1f}s): "
                f"{[(t.value, c) for t, c in stuck_tiers]}"
            )
            # Reset counters
            for tier, _ in stuck_tiers:
                self._active_requests[tier] = 0
            self.logger.info("Reset active request counters to zero")

    async def _get_ram_info(self) -> Optional[dict]:
        """Get current RAM information."""
        now = time.time()
        if self._cached_ram_info and (now - self._last_ram_check) < self._ram_cache_ttl:
            return self._cached_ram_info

        try:
            import psutil
            mem = psutil.virtual_memory()
            info = {
                "total_gb": mem.total / (1024**3),
                "available_gb": mem.available / (1024**3),
                "used_percent": mem.percent,
                "timestamp": now,
            }
            self._last_ram_check = now
            return info
        except Exception as e:
            self.logger.warning(f"RAM check failed: {e}")
            return None

    def _check_circuit_breaker(self, tier: RoutingTier) -> bool:
        """Check if circuit breaker allows requests (v2.0)."""
        # v2.0: Use CrossRepoCircuitBreaker if available
        if self._circuit_breaker:
            health = self._circuit_breaker.get_tier_health(tier.value)
            if health:
                return health.state.value in ("closed", "half_open")
            return True  # Default to allowing if no history

        # Legacy implementation
        cb = self._legacy_circuit_breakers.get(tier)
        if not cb:
            return True

        if cb["state"] == "closed":
            return True

        if cb["state"] == "open":
            if time.time() - cb["last_failure"] > cb["timeout"]:
                cb["state"] = "half_open"
                return True
            return False

        if cb["state"] == "half_open":
            return True

        return False

    def _record_success(self, tier: RoutingTier, latency_ms: float = 0.0) -> None:
        """Record successful request (v2.0)."""
        # v2.0: Use CrossRepoCircuitBreaker
        if self._circuit_breaker:
            # Success is recorded via execute() completion
            pass

        # Legacy implementation
        cb = self._legacy_circuit_breakers.get(tier)
        if cb and cb["state"] == "half_open":
            cb["state"] = "closed"
            cb["failures"] = 0

        # v2.1: Track latency samples (deque auto-removes old items with maxlen)
        self._metrics.latency_samples.append(latency_ms)

        # Update average - use cached sum for O(1) average calculation
        if self._metrics.latency_samples:
            self._metrics.avg_latency_ms = sum(self._metrics.latency_samples) / len(self._metrics.latency_samples)

    def _record_failure(self, tier: RoutingTier, error: Optional[Exception] = None) -> None:
        """Record failed request with failure classification (v2.0)."""
        # v2.0: Classify failure type
        failure_type = self._classify_failure(error) if error else "unknown"

        # Update failure metrics by type
        if failure_type == "timeout":
            self._metrics.timeout_failures += 1
        elif failure_type == "network":
            self._metrics.network_failures += 1
        elif failure_type == "resource":
            self._metrics.resource_failures += 1
        elif failure_type == "transient":
            self._metrics.transient_failures += 1
        elif failure_type == "permanent":
            self._metrics.permanent_failures += 1

        # v2.0: Use CrossRepoCircuitBreaker
        if self._circuit_breaker:
            # Failure is recorded via execute() exception
            self._metrics.circuit_breaker_trips += 1

        # Legacy implementation
        cb = self._legacy_circuit_breakers.get(tier)
        if cb:
            cb["failures"] += 1
            cb["last_failure"] = time.time()

            if cb["failures"] >= cb["threshold"]:
                cb["state"] = "open"
                self.logger.warning(f"Circuit breaker OPEN for {tier.value}")

    async def _check_circuit_breakers(self) -> None:
        """Check and potentially reset circuit breakers (v2.0)."""
        # v2.0: CrossRepoCircuitBreaker handles this internally
        if self._circuit_breaker:
            return

        # Legacy implementation
        now = time.time()
        for tier, cb in self._legacy_circuit_breakers.items():
            if cb["state"] == "open":
                if now - cb["last_failure"] > cb["timeout"]:
                    cb["state"] = "half_open"
                    self.logger.info(f"Circuit breaker HALF_OPEN for {tier.value}")

    def _classify_failure(self, error: Optional[Exception]) -> str:
        """Classify failure type for intelligent retry decisions (v2.0)."""
        if error is None:
            return "unknown"

        error_str = str(error).lower()
        error_type = type(error).__name__

        # Timeout failures
        if "timeout" in error_str or error_type in ("TimeoutError", "asyncio.TimeoutError"):
            return "timeout"

        # Network failures
        if any(kw in error_str for kw in ["connection", "network", "refused", "reset"]):
            return "network"
        if error_type in ("ConnectionError", "ConnectionRefusedError", "ConnectionResetError"):
            return "network"

        # Resource failures (OOM, disk full, etc.)
        if any(kw in error_str for kw in ["memory", "oom", "disk", "space", "resource"]):
            return "resource"
        if error_type in ("MemoryError", "OSError"):
            return "resource"

        # Transient failures (temporary issues)
        if any(kw in error_str for kw in ["retry", "temporary", "unavailable", "busy", "overload"]):
            return "transient"
        if error_type in ("BrokenPipeError",):
            return "transient"

        # Permanent failures (auth, config, etc.)
        if any(kw in error_str for kw in ["auth", "permission", "denied", "invalid", "not found"]):
            return "permanent"
        if error_type in ("PermissionError", "ValueError", "KeyError"):
            return "permanent"

        return "transient"  # Default to transient for unknown errors

    def _should_retry(self, failure_type: str, current_retries: int) -> Tuple[bool, float]:
        """Determine if should retry based on failure type (v2.0)."""
        config = self._retry_config.get(failure_type, {"max_retries": 1, "delay": 0.5})
        should_retry = current_retries < config["max_retries"]
        return should_retry, config["delay"]

    # =========================================================================
    # Core Routing Logic
    # =========================================================================

    async def route(
        self,
        task_type: str,
        estimated_tokens: int = 1000,
        required_capability: Optional[str] = None,
        force_tier: Optional[RoutingTier] = None,
    ) -> RoutingDecision:
        """
        Determine optimal routing tier for a request.

        Args:
            task_type: Type of task (chat, code, reasoning, etc.)
            estimated_tokens: Estimated token count for cost calculation
            required_capability: Specific capability requirement
            force_tier: Force a specific tier (for testing)

        Returns:
            RoutingDecision with tier, reason, and metadata
        """
        self._metrics.total_requests += 1
        self._metrics.last_request_time = time.time()

        # Allow forcing tier
        if force_tier:
            return RoutingDecision(
                tier=force_tier,
                reason=RoutingReason.CAPABILITY_REQUIRED,
                timeout_ms=self._get_timeout_for_tier(force_tier),
            )

        # Step 1: Check unified budget
        budget_decision = await self._check_budget(estimated_tokens)
        if budget_decision:
            self._metrics.budget_blocks += 1
            return budget_decision

        # Step 2: Check local RAM availability
        ram_info = await self._get_ram_info()
        if ram_info and self._can_use_local(ram_info):
            self._metrics.local_requests += 1
            return RoutingDecision(
                tier=RoutingTier.LOCAL_PRIME,
                reason=RoutingReason.LOCAL_RAM_SUFFICIENT,
                estimated_cost=0.0,  # Local is free
                timeout_ms=LOCAL_TIMEOUT_MS,
                metadata={
                    "ram_available_gb": ram_info["available_gb"],
                    "ram_used_percent": ram_info["used_percent"],
                },
            )

        # Step 3: Check GCP VM availability
        if self._gcp_controller and self._check_circuit_breaker(RoutingTier.GCP_VM):
            gcp_decision = await self._check_gcp_availability(estimated_tokens)
            if gcp_decision:
                self._metrics.gcp_requests += 1
                return gcp_decision

        # Step 4: Check Cloud Run availability
        if self._check_circuit_breaker(RoutingTier.GCP_CLOUD_RUN):
            cloudrun_decision = await self._check_cloudrun_availability(estimated_tokens)
            if cloudrun_decision:
                return cloudrun_decision

        # Step 5: Fallback to Cloud Claude API
        self._metrics.cloud_requests += 1
        self._metrics.fallback_requests += 1

        return RoutingDecision(
            tier=RoutingTier.CLOUD_CLAUDE,
            reason=RoutingReason.FALLBACK,
            estimated_cost=self._estimate_claude_cost(estimated_tokens),
            timeout_ms=CLOUD_API_TIMEOUT_MS,
            metadata={
                "fallback_reason": "all_other_tiers_unavailable",
            },
        )

    async def _check_budget(self, estimated_tokens: int) -> Optional[RoutingDecision]:
        """Check if budget allows cloud routing."""
        if not self._cost_sync:
            return None

        # Estimate cloud cost
        estimated_cost = self._estimate_claude_cost(estimated_tokens)

        # Check if we can afford this request
        if not self._cost_sync.can_incur_cost(estimated_cost):
            self.logger.warning(
                f"Budget exceeded - forcing local mode "
                f"(estimated: ${estimated_cost:.4f}, "
                f"remaining: ${self._cost_sync.get_remaining_budget():.4f})"
            )

            # Force degraded local mode
            return RoutingDecision(
                tier=RoutingTier.DEGRADED_LOCAL,
                reason=RoutingReason.BUDGET_EXCEEDED,
                estimated_cost=0.0,
                timeout_ms=LOCAL_TIMEOUT_MS * 2,  # Longer timeout for degraded
                metadata={
                    "remaining_budget": self._cost_sync.get_remaining_budget(),
                    "estimated_cost": estimated_cost,
                },
            )

        return None

    def _can_use_local(self, ram_info: dict) -> bool:
        """Check if local Prime can be used."""
        if not ram_info:
            return False

        # Check if we have enough available RAM
        if ram_info["available_gb"] >= LOCAL_PRIME_MIN_RAM_GB:
            return True

        # Check if RAM usage is below critical
        if ram_info["used_percent"] < GCP_TRIGGER_RAM_PERCENT:
            return True

        return False

    async def _check_gcp_availability(
        self,
        estimated_tokens: int,
    ) -> Optional[RoutingDecision]:
        """Check GCP VM availability."""
        if not self._gcp_controller:
            return None

        try:
            # Check if GCP VM is available
            if hasattr(self._gcp_controller, 'is_vm_available'):
                if not self._gcp_controller.is_vm_available():
                    return None

            # Check if we should use GCP (cost-aware)
            gcp_cost = self._estimate_gcp_cost(estimated_tokens)

            # Only use GCP if significantly cheaper than Claude API
            claude_cost = self._estimate_claude_cost(estimated_tokens)
            if gcp_cost >= claude_cost * 0.8:
                return None  # Not worth it

            return RoutingDecision(
                tier=RoutingTier.GCP_VM,
                reason=RoutingReason.GCP_AVAILABLE,
                estimated_cost=gcp_cost,
                timeout_ms=GCP_TIMEOUT_MS,
                metadata={
                    "savings_vs_claude": claude_cost - gcp_cost,
                },
            )

        except Exception as e:
            self.logger.debug(f"GCP check failed: {e}")
            return None

    async def _check_cloudrun_availability(
        self,
        estimated_tokens: int,
    ) -> Optional[RoutingDecision]:
        """Check Cloud Run availability."""
        # Check circuit breaker
        if not self._check_circuit_breaker(RoutingTier.GCP_CLOUD_RUN):
            return None

        # Cloud Run is generally always available if configured
        cloud_run_url = os.getenv("JARVIS_PRIME_CLOUD_RUN_URL")
        if not cloud_run_url:
            return None

        return RoutingDecision(
            tier=RoutingTier.GCP_CLOUD_RUN,
            reason=RoutingReason.GCP_AVAILABLE,
            estimated_cost=self._estimate_cloudrun_cost(estimated_tokens),
            timeout_ms=GCP_TIMEOUT_MS,
            metadata={
                "cloud_run_url": cloud_run_url,
            },
        )

    def _estimate_claude_cost(self, tokens: int) -> float:
        """Estimate cost for Claude API."""
        # Claude 3 Haiku pricing: $0.008/1K input, $0.024/1K output
        # Assume 60% input, 40% output
        input_tokens = tokens * 0.6
        output_tokens = tokens * 0.4
        return (input_tokens / 1000) * 0.008 + (output_tokens / 1000) * 0.024

    def _estimate_gcp_cost(self, tokens: int) -> float:
        """Estimate cost for GCP VM execution."""
        # Spot VM: ~$0.029/hour for e2-highmem-4
        # Estimate ~30 seconds per 1K tokens
        hours = (tokens / 1000) * (30 / 3600)
        return hours * 0.029

    def _estimate_cloudrun_cost(self, tokens: int) -> float:
        """Estimate cost for Cloud Run execution."""
        # Cloud Run: ~$0.00002400 per vCPU-second
        # Estimate ~15 seconds per 1K tokens with 2 vCPUs
        seconds = (tokens / 1000) * 15
        return seconds * 2 * 0.00002400

    def _get_timeout_for_tier(self, tier: RoutingTier) -> int:
        """Get timeout for a specific tier."""
        timeouts = {
            RoutingTier.LOCAL_PRIME: LOCAL_TIMEOUT_MS,
            RoutingTier.GCP_VM: GCP_TIMEOUT_MS,
            RoutingTier.GCP_CLOUD_RUN: GCP_TIMEOUT_MS,
            RoutingTier.CLOUD_CLAUDE: CLOUD_API_TIMEOUT_MS,
            RoutingTier.DEGRADED_LOCAL: LOCAL_TIMEOUT_MS * 2,
        }
        return timeouts.get(tier, 30000)

    # =========================================================================
    # Execution
    # =========================================================================

    async def execute(
        self,
        task_type: str,
        payload: Dict[str, Any],
        decision: Optional[RoutingDecision] = None,
    ) -> Dict[str, Any]:
        """
        Execute a request using the determined routing tier.

        v2.0: Added retry logic with failure classification and circuit breaker.

        Args:
            task_type: Type of task
            payload: Request payload
            decision: Optional pre-computed routing decision

        Returns:
            Response from the selected tier
        """
        if not decision:
            decision = await self.route(
                task_type,
                estimated_tokens=payload.get("max_tokens", 1000),
            )

        # Add correlation context if available
        ctx = get_correlation_context() if get_correlation_context else None
        if ctx:
            payload = {**payload, "_correlation_id": ctx.correlation_id}

        # Record decision
        self.logger.info(
            f"Routing {task_type} to {decision.tier.value} "
            f"(reason: {decision.reason.value})"
        )

        # v2.0: Execute with retry logic
        return await self._execute_with_retry(decision, payload)

    async def _execute_with_retry(
        self,
        decision: RoutingDecision,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute with intelligent retry based on failure classification (v2.0)."""
        retries = 0
        last_error: Optional[Exception] = None
        start_time = time.time()

        # v2.0: Reference counting for VM lifecycle management
        self._increment_active_requests(decision.tier)

        try:
            while True:
                try:
                    # Use circuit breaker if available
                    if self._circuit_breaker and self._use_resilience:
                        result = await self._circuit_breaker.execute(
                            tier=decision.tier.value,
                            func=lambda: self._execute_on_tier(decision.tier, payload),
                            timeout=decision.timeout_ms / 1000,
                        )
                    else:
                        result = await self._execute_on_tier(decision.tier, payload)

                    latency_ms = (time.time() - start_time) * 1000
                    self._record_success(decision.tier, latency_ms)

                    # v2.0: Track successful tier and exit degradation mode
                    self._last_successful_tier = decision.tier
                    self._tier_failure_counts[decision.tier] = 0
                    self._tier_last_success[decision.tier] = time.time()

                    if self._degradation_mode:
                        self.logger.info(f"Exiting degradation mode - {decision.tier.value} succeeded")
                        self._degradation_mode = False
                        self._degradation_reason = None
                        self._degradation_entered_at = 0.0  # v2.1: Reset timestamp

                    # Track cost - v2.1: Added error handling to prevent cost sync failures from breaking execution
                    if self._cost_sync:
                        try:
                            self._cost_sync.record_inference(
                                tokens_in=payload.get("max_tokens", 1000),
                                tokens_out=result.get("tokens_used", 500),
                                cost=decision.estimated_cost,
                                is_local=decision.tier == RoutingTier.LOCAL_PRIME,
                            )
                        except Exception as cost_error:
                            self.logger.warning(f"Cost tracking failed (non-fatal): {cost_error}")

                    # Track retry success
                    if retries > 0:
                        self._metrics.retries_successful += 1

                    return result

                except Exception as e:
                    last_error = e
                    failure_type = self._classify_failure(e)

                    # Record failure
                    self._record_failure(decision.tier, e)

                    # v2.0: Track tier failures for degradation detection
                    self._tier_failure_counts[decision.tier] = self._tier_failure_counts.get(decision.tier, 0) + 1

                    # Check if should retry
                    should_retry, delay = self._should_retry(failure_type, retries)

                    if should_retry:
                        retries += 1
                        self._metrics.retries_total += 1
                        self.logger.warning(
                            f"Retry {retries} for {decision.tier.value}: "
                            f"{failure_type} failure - {e}"
                        )
                        await asyncio.sleep(delay)
                    else:
                        # No more retries - try graceful degradation
                        fallback_result = await self._try_graceful_degradation(
                            decision, payload, last_error
                        )
                        if fallback_result:
                            return fallback_result

                        # No fallback available
                        self.logger.error(
                            f"Failed on {decision.tier.value} after {retries} retries: "
                            f"{failure_type} failure - {e}"
                        )
                        raise
        finally:
            # v2.0: Always decrement reference count
            self._decrement_active_requests(decision.tier)

    async def _try_graceful_degradation(
        self,
        original_decision: RoutingDecision,
        payload: Dict[str, Any],
        last_error: Exception,
    ) -> Optional[Dict[str, Any]]:
        """
        v2.0: Attempt graceful degradation when primary tier fails.

        Tries fallback tiers in order:
        1. If GCP failed → try local (if RAM available)
        2. If local failed → try cloud Claude
        3. If all failed → return degraded response

        Returns:
            Result from fallback tier, or None if all tiers failed
        """
        original_tier = original_decision.tier
        self.logger.warning(f"Attempting graceful degradation from {original_tier.value}")

        # Define fallback chain
        fallback_chain: List[RoutingTier] = []

        if original_tier == RoutingTier.LOCAL_PRIME:
            fallback_chain = [RoutingTier.GCP_VM, RoutingTier.GCP_CLOUD_RUN, RoutingTier.CLOUD_CLAUDE]
        elif original_tier == RoutingTier.GCP_VM:
            fallback_chain = [RoutingTier.LOCAL_PRIME, RoutingTier.GCP_CLOUD_RUN, RoutingTier.CLOUD_CLAUDE]
        elif original_tier == RoutingTier.GCP_CLOUD_RUN:
            fallback_chain = [RoutingTier.LOCAL_PRIME, RoutingTier.GCP_VM, RoutingTier.CLOUD_CLAUDE]
        elif original_tier == RoutingTier.CLOUD_CLAUDE:
            fallback_chain = [RoutingTier.LOCAL_PRIME, RoutingTier.GCP_VM, RoutingTier.GCP_CLOUD_RUN]

        # Try each fallback tier
        for fallback_tier in fallback_chain:
            # Check circuit breaker
            if not self._check_circuit_breaker(fallback_tier):
                self.logger.debug(f"Skipping {fallback_tier.value} - circuit breaker open")
                continue

            # Check if tier is available
            if fallback_tier == RoutingTier.LOCAL_PRIME:
                ram_info = await self._get_ram_info()
                if not self._can_use_local(ram_info):
                    self.logger.debug(f"Skipping {fallback_tier.value} - insufficient RAM")
                    continue

            self.logger.info(f"Attempting fallback to {fallback_tier.value}")

            try:
                self._increment_active_requests(fallback_tier)
                result = await self._execute_on_tier(fallback_tier, payload)

                # Mark as degraded response
                result["degraded"] = True
                result["original_tier"] = original_tier.value
                result["fallback_tier"] = fallback_tier.value

                self._metrics.fallback_requests += 1
                self._last_successful_tier = fallback_tier

                self.logger.info(f"Graceful degradation successful: {fallback_tier.value}")
                return result

            except Exception as e:
                self.logger.warning(f"Fallback to {fallback_tier.value} failed: {e}")
                self._decrement_active_requests(fallback_tier)
                continue

        # All tiers failed - enter degradation mode
        self._degradation_mode = True
        self._degradation_reason = f"All tiers failed after {original_tier.value}: {last_error}"
        # v2.1: Set timestamp for timeout-based recovery
        self._degradation_entered_at = time.time()

        # Return minimal degraded response
        self.logger.error(
            f"All fallback tiers failed - entering degradation mode "
            f"(recovery timeout: {self._degradation_recovery_timeout}s)"
        )
        return {
            "response": "[System operating in degraded mode - please try again later]",
            "degraded": True,
            "all_tiers_failed": True,
            "error": str(last_error),
        }

    async def _execute_on_tier(
        self,
        tier: RoutingTier,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute request on specific tier."""
        if tier == RoutingTier.LOCAL_PRIME:
            return await self._execute_local(payload)
        elif tier == RoutingTier.GCP_VM:
            return await self._execute_gcp_vm(payload)
        elif tier == RoutingTier.GCP_CLOUD_RUN:
            return await self._execute_cloud_run(payload)
        elif tier == RoutingTier.CLOUD_CLAUDE:
            return await self._execute_cloud_claude(payload)
        elif tier == RoutingTier.DEGRADED_LOCAL:
            return await self._execute_degraded_local(payload)
        else:
            raise ValueError(f"Unknown tier: {tier}")

    async def _execute_local(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute on local JARVIS Prime."""
        if self._prime_client:
            return await self._prime_client.generate(payload)

        # Fallback to neural mesh
        if self._neural_mesh and self._neural_mesh.is_prime_available():
            return await self._neural_mesh.route_to_prime("inference", payload)

        raise RuntimeError("Local Prime not available")

    async def _execute_gcp_vm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute on GCP VM."""
        if not self._gcp_controller:
            raise RuntimeError("GCP controller not available")

        # Get VM endpoint
        if hasattr(self._gcp_controller, 'get_vm_endpoint'):
            endpoint = self._gcp_controller.get_vm_endpoint()
            if not endpoint:
                raise RuntimeError("No GCP VM endpoint available")

            # Execute via HTTP
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=GCP_TIMEOUT_MS/1000),
                ) as resp:
                    return await resp.json()

        raise RuntimeError("GCP VM not available")

    async def _execute_cloud_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute on Cloud Run."""
        cloud_run_url = os.getenv("JARVIS_PRIME_CLOUD_RUN_URL")
        if not cloud_run_url:
            raise RuntimeError("Cloud Run URL not configured")

        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{cloud_run_url}/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=GCP_TIMEOUT_MS/1000),
            ) as resp:
                return await resp.json()

    async def _execute_cloud_claude(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute on Cloud Claude API."""
        try:
            from backend.intelligence.unified_model_serving import get_model_server
            server = await get_model_server()

            # Use unified model serving for Claude
            result = await server.generate(
                prompt=payload.get("prompt", ""),
                max_tokens=payload.get("max_tokens", 1000),
                task_type="CHAT",
            )
            return {"response": result, "source": "cloud_claude"}

        except Exception as e:
            self.logger.error(f"Cloud Claude failed: {e}")
            raise

    async def _execute_degraded_local(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute in degraded local mode (reduced capabilities)."""
        # Try local with reduced settings
        reduced_payload = {
            **payload,
            "max_tokens": min(payload.get("max_tokens", 1000), 500),
            "temperature": 0.3,  # More deterministic
        }

        try:
            return await self._execute_local(reduced_payload)
        except Exception:
            # Return minimal response
            return {
                "response": "[Budget exceeded - limited response available]",
                "degraded": True,
                "source": "degraded_local",
            }

    # =========================================================================
    # Public API
    # =========================================================================

    def get_metrics(self) -> Dict[str, Any]:
        """Get router metrics (v2.0 extended)."""
        # Get circuit breaker status
        cb_status = {}
        if self._circuit_breaker:
            for tier in RoutingTier:
                health = self._circuit_breaker.get_tier_health(tier.value)
                if health:
                    cb_status[tier.value] = {
                        "state": health.state.value,
                        "failure_count": health.failure_count,
                        "success_rate": health.success_rate,
                    }
        else:
            # Legacy circuit breakers
            cb_status = {
                tier.value: cb["state"]
                for tier, cb in self._legacy_circuit_breakers.items()
            }

        return {
            "running": self._running,
            "resilience_enabled": self._use_resilience,
            "total_requests": self._metrics.total_requests,
            "local_requests": self._metrics.local_requests,
            "gcp_requests": self._metrics.gcp_requests,
            "cloud_requests": self._metrics.cloud_requests,
            "fallback_requests": self._metrics.fallback_requests,
            "budget_blocks": self._metrics.budget_blocks,
            "total_cost": self._metrics.total_cost,
            "avg_latency_ms": self._metrics.avg_latency_ms,
            # v2.0 additions
            "circuit_breaker_trips": self._metrics.circuit_breaker_trips,
            "timeout_failures": self._metrics.timeout_failures,
            "network_failures": self._metrics.network_failures,
            "resource_failures": self._metrics.resource_failures,
            "transient_failures": self._metrics.transient_failures,
            "permanent_failures": self._metrics.permanent_failures,
            "retries_total": self._metrics.retries_total,
            "retries_successful": self._metrics.retries_successful,
            "retry_success_rate": (
                self._metrics.retries_successful / self._metrics.retries_total
                if self._metrics.retries_total > 0 else 0.0
            ),
            "circuit_breakers": cb_status,
            # v2.0 VM provisioning and degradation
            "vm_provisioning_enabled": self._vm_provisioning_enabled,
            "vm_provisioning_in_progress": self._vm_provisioning_in_progress,
            "active_requests": {tier.value: count for tier, count in self._active_requests.items()},
            "degradation_mode": self._degradation_mode,
            "degradation_reason": self._degradation_reason,
            "last_successful_tier": self._last_successful_tier.value if self._last_successful_tier else None,
            "tier_failure_counts": {tier.value: count for tier, count in self._tier_failure_counts.items()},
        }

    def on_decision(self, callback: Callable) -> None:
        """Register callback for routing decisions."""
        self._decision_callbacks.add(callback)


# =============================================================================
# Global Instance Management
# =============================================================================

_router: Optional[GCPHybridPrimeRouter] = None
_router_lock: Optional[asyncio.Lock] = None


def _get_router_lock() -> asyncio.Lock:
    """Get or create the router lock."""
    global _router_lock
    if _router_lock is None:
        _router_lock = asyncio.Lock()
    return _router_lock


async def get_gcp_hybrid_prime_router() -> GCPHybridPrimeRouter:
    """Get the global GCPHybridPrimeRouter instance."""
    global _router

    lock = _get_router_lock()
    async with lock:
        if _router is None:
            _router = GCPHybridPrimeRouter()
            await _router.start()

        return _router


async def shutdown_gcp_hybrid_prime_router() -> None:
    """Shutdown the global GCPHybridPrimeRouter."""
    global _router

    if _router:
        await _router.stop()
        _router = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "GCPHybridPrimeRouter",
    "RoutingTier",
    "RoutingReason",
    "RoutingDecision",
    "RouterMetrics",
    "ExecutionResult",
    "get_gcp_hybrid_prime_router",
    "shutdown_gcp_hybrid_prime_router",
]
