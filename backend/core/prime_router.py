"""
Prime Router v1.0
=================

Central AI inference router that routes all requests through JARVIS-Prime
with automatic fallback to cloud Claude API.

This module is the KEY INTEGRATION POINT that connects JARVIS to its Trinity
architecture. All AI inference requests flow through here.

ARCHITECTURE:
    User Request
         ↓
    PrimeRouter
         ↓
    ┌──────────────────────────────────────────┐
    │  Route Decision (based on health/config) │
    └──────────────────────────────────────────┘
         ↓                    ↓
    LOCAL PRIME          CLOUD CLAUDE
    (Free, Fast)         (Paid, Reliable)
         ↓                    ↓
    ┌──────────────────────────────────────────┐
    │            Response Fusion               │
    │   (Metrics, Logging, Graceful Degrade)   │
    └──────────────────────────────────────────┘
         ↓
    User Response

USAGE:
    from backend.core.prime_router import get_prime_router

    router = await get_prime_router()

    # Generate response (auto-routes to best available backend)
    response = await router.generate(
        prompt="What is the weather?",
        system_prompt="You are JARVIS."
    )

    # Check routing status
    status = router.get_status()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, AsyncGenerator

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# =============================================================================
# P0-2: Disease 10 boot routing policy wire-in
# =============================================================================
# The supervisor calls wire_boot_routing_policy() once after it creates the
# StartupOrchestrator so that routing decisions during boot honour the
# deadline-based fallback logic (StartupRoutingPolicy).
#
# The policy is consulted only while it is NOT yet finalised.  Once the
# supervisor calls policy.finalize() (post CORE_READY gate), the policy
# locks its decision and this gate is bypassed entirely.
#
# This is intentionally a module-level singleton (not per-PrimeRouter
# instance) because a single supervisor manages a single routing policy.
_g_boot_routing_policy: Optional[Any] = None


def wire_boot_routing_policy(policy: Any) -> None:
    """Wire the Disease 10 StartupRoutingPolicy into the prime router.

    Must be called by the supervisor after it creates the StartupOrchestrator.
    Thread-safe: Python's GIL makes the assignment atomic for CPython.
    """
    global _g_boot_routing_policy
    _g_boot_routing_policy = policy
    logger.info("[PrimeRouter] Boot routing policy wired: %s", type(policy).__name__)

# =============================================================================
# v241.0: DEADLINE PROPAGATION
# =============================================================================
# Monotonic deadline flows from WebSocket through all layers.
# Each layer computes remaining = deadline - monotonic() and caps its timeout.
# Inner layers self-terminate before the outer deadline, preventing destructive
# asyncio.wait_for() cancellations.

# v242.0: Headroom subtracted ONCE at deadline creation (unified_websocket.py).
# Inner layers just compute (deadline - now). No per-layer compounding.
try:
    _DEADLINE_HEADROOM_S = float(os.getenv("JARVIS_DEADLINE_HEADROOM_S", "2.0"))
except ValueError:
    _DEADLINE_HEADROOM_S = 2.0


def compute_remaining(deadline: Optional[float], own_timeout: float) -> float:
    """Effective timeout = min(own_timeout, deadline_remaining). Returns >= 0.5."""
    if deadline is None:
        return own_timeout
    remaining = deadline - time.monotonic()
    return max(min(own_timeout, remaining), 0.5)


class _EndpointAwareCircuitBreaker:
    """v242.0: Endpoint-aware circuit breaker. Resets on endpoint change."""

    def __init__(self, threshold: int = 2, recovery_s: float = 30.0):
        # v270.4: Pull from unified recovery policy (was threshold=2, too aggressive)
        try:
            from backend.core.recovery_policy import get_recovery_params
            _rp = get_recovery_params("prime_router")
            if _rp is not None:
                threshold = _rp.circuit_failure_threshold
                recovery_s = _rp.circuit_recovery_seconds
        except ImportError:
            pass
        self._threshold = threshold
        self._recovery_s = recovery_s
        self._failures = 0
        self._last_failure = 0.0
        self._state = "cold"  # cold | closed | open | half_open
        self._endpoint_id: Optional[str] = None
        self._endpoint_promoted = False

    def reset_for_endpoint(self, endpoint_id: str, health_checked: bool) -> None:
        """Reset state on endpoint change. health_checked=True skips cold probe."""
        self._failures = 0
        self._last_failure = 0.0
        self._endpoint_id = endpoint_id
        self._endpoint_promoted = health_checked
        self._state = "closed" if health_checked else "cold"
        logger.info(f"[PrimeRouter] v242.0 Circuit reset for {endpoint_id} "
                     f"(state={'closed' if health_checked else 'cold'})")

    def can_execute(self) -> bool:
        if self._state in ("closed", "cold"):
            return True
        if self._state == "open":
            if time.monotonic() - self._last_failure >= self._recovery_s:
                self._state = "half_open"
                return True
            return False
        return True  # half_open

    def get_timeout_override(self, default_timeout: float, is_cloud_run: bool = False) -> float:
        """Probe timeout for cold/half_open endpoints.

        Cloud Run endpoints get longer timeouts to tolerate cold starts (10-30s).
        """
        if self._state in ("cold", "half_open") and not self._endpoint_promoted:
            if is_cloud_run:
                probe_s = _get_env_float("JARVIS_CLOUD_RUN_PROBE_TIMEOUT", 45.0)
            else:
                probe_s = _get_env_float("PRIME_PROBE_TIMEOUT_S", 5.0)
            return min(probe_s, default_timeout) if not is_cloud_run else probe_s
        return default_timeout

    def record_success(self):
        self._failures = 0
        self._state = "closed"

    def record_failure(self, endpoint_id: Optional[str] = None):
        """Record failure. Discards stale failures from old endpoints."""
        if endpoint_id and self._endpoint_id and endpoint_id != self._endpoint_id:
            logger.debug(f"[PrimeRouter] v242.0 Discarding stale failure "
                          f"(from={endpoint_id}, current={self._endpoint_id})")
            return
        self._failures += 1
        self._last_failure = time.monotonic()
        if self._state == "cold" or self._failures >= self._threshold:
            self._state = "open"
            logger.info(f"[PrimeRouter] v242.0 Circuit OPEN after {self._failures} failures "
                         f"(endpoint={self._endpoint_id})")


# =============================================================================
# v88.0: ULTRA COORDINATOR INTEGRATION
# =============================================================================

# v88.0: Module-level ultra coordinator for protection
_ultra_coordinator: Optional[Any] = None
_ultra_coord_lock: Optional[asyncio.Lock] = None


async def _get_ultra_coordinator() -> Optional[Any]:
    """v88.0: Get ultra coordinator with lazy initialization."""
    global _ultra_coordinator, _ultra_coord_lock

    # Skip if disabled
    if os.getenv("JARVIS_ENABLE_ULTRA_COORD", "true").lower() not in ("true", "1", "yes"):
        return None

    if _ultra_coordinator is not None:
        return _ultra_coordinator

    # Lazy init lock
    if _ultra_coord_lock is None:
        _ultra_coord_lock = asyncio.Lock()

    async with _ultra_coord_lock:
        if _ultra_coordinator is not None:
            return _ultra_coordinator

        try:
            from backend.core.trinity_integrator import get_ultra_coordinator
            _ultra_coordinator = await get_ultra_coordinator()
            logger.info("[PrimeRouter] v88.0 Ultra coordinator initialized")
            return _ultra_coordinator
        except Exception as e:
            logger.debug(f"[PrimeRouter] v88.0 Ultra coordinator not available: {e}")
            return None


# =============================================================================
# CONFIGURATION
# =============================================================================

def _get_env_bool(key: str, default: bool) -> bool:
    """Get bool from environment."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _get_env_float(key: str, default: float) -> float:
    """Get float from environment."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


class MirrorModeError(RuntimeError):
    """Raised when a mutating method is called in mirror mode."""


@dataclass
class PrimeRouterConfig:
    """Configuration for the Prime router."""
    # Routing strategy
    prefer_local: bool = field(default_factory=lambda: _get_env_bool("PRIME_PREFER_LOCAL", True))
    enable_cloud_fallback: bool = field(default_factory=lambda: _get_env_bool("PRIME_ENABLE_CLOUD_FALLBACK", True))
    enable_metrics: bool = field(default_factory=lambda: _get_env_bool("PRIME_ENABLE_METRICS", True))

    # Performance thresholds
    local_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_LOCAL_TIMEOUT", 30.0))
    cloud_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_CLOUD_TIMEOUT", 60.0))
    # v235.4: GCP VM uses CPU inference (~25-35s). 30s local_timeout always times out.
    # Separate timeout for GCP-routed requests to accommodate slower CPU inference.
    gcp_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_GCP_TIMEOUT", 120.0))

    # Health thresholds
    min_local_health: float = field(default_factory=lambda: _get_env_float("PRIME_MIN_LOCAL_HEALTH", 0.5))


class RoutingDecision(Enum):
    """Routing decision types."""
    LOCAL_PRIME = "local_prime"
    GCP_PRIME = "gcp_prime"  # v232.0: GCP VM endpoint (promoted)
    CLOUD_CLAUDE = "cloud_claude"
    HYBRID = "hybrid"  # Try local first, then cloud
    CACHED = "cached"
    DEGRADED = "degraded"


@dataclass
class RoutingMetrics:
    """Metrics for routing decisions."""
    total_requests: int = 0
    local_requests: int = 0
    cloud_requests: int = 0
    fallback_count: int = 0
    total_latency_ms: float = 0.0
    local_latency_ms: float = 0.0
    cloud_latency_ms: float = 0.0
    errors: int = 0

    @property
    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests

    @property
    def local_ratio(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.local_requests / self.total_requests


@dataclass
class RouterResponse:
    """Response from the router."""
    content: str
    source: str  # local_prime, cloud_claude, cached, degraded
    latency_ms: float
    model: str
    tokens_used: int = 0
    fallback_used: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# PRIME ROUTER
# =============================================================================

class PrimeRouter:
    """
    Central router for all AI inference requests.

    Routes between local JARVIS-Prime and cloud Claude API based on:
    - Health status of components
    - User preferences
    - Cost optimization
    - Performance requirements
    """

    def __init__(self, config: Optional[PrimeRouterConfig] = None):
        self._config = config or PrimeRouterConfig()
        self._metrics = RoutingMetrics()
        self._prime_client = None
        self._cloud_client = None
        self._graceful_degradation = None
        self._lock = asyncio.Lock()
        self._initialized = False
        # v232.0: GCP VM promotion state
        self._gcp_promoted = False
        self._gcp_host: Optional[str] = None
        self._gcp_port: Optional[int] = None
        # v242.0: Endpoint-aware circuit breaker (resets on endpoint change)
        self._local_circuit = _EndpointAwareCircuitBreaker()
        # v271.0: Flapping protection — minimum cooldown between promote/demote transitions
        self._last_transition_time: float = 0.0
        self._transition_cooldown_s: float = float(
            os.environ.get("JARVIS_ROUTING_TRANSITION_COOLDOWN_S", "30.0")
        )
        # In-flight flag: prevents concurrent promote/demote while network I/O
        # is in progress outside the lock
        self._transition_in_flight: bool = False
        # Disease 10: Mirror mode — blocks all mutating methods when active
        self._mirror_mode: bool = False
        self._mirror_decisions_issued: int = 0
        # Cloud Run endpoint detection patterns
        self._cloud_run_patterns = (".run.app", ".a.run.app")

    def _is_cloud_run_endpoint(self, host: Optional[str] = None) -> bool:
        """Detect if the given (or current GCP) host is a Cloud Run endpoint."""
        h = host or self._gcp_host or ""
        return any(h.endswith(pat) for pat in self._cloud_run_patterns)

    @staticmethod
    def _classify_protection_error(metadata: Dict[str, Any]) -> str:
        """Map protection metadata to a stable degraded error code."""
        if metadata.get("error_code"):
            return str(metadata["error_code"])
        if metadata.get("circuit_open"):
            return "circuit_open"
        if metadata.get("backpressure_dropped"):
            return "backpressure_rejected"
        if metadata.get("timeout"):
            return "timeout"
        return "operation_failed"

    @staticmethod
    def _build_degraded_metadata(
        *,
        error_code: str,
        error_message: str,
        origin_layer: str,
        retryable: bool,
        trace_id: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Build a normalized degraded metadata envelope with compatibility aliases."""
        metadata: Dict[str, Any] = {
            "error_code": error_code,
            "error_message": error_message,
            "error": error_message,
            "reason": error_code,
            "origin_layer": origin_layer,
            "retryable": retryable,
            "v88_error": error_message,
        }
        if trace_id:
            metadata["trace_id"] = trace_id
        for key, value in extra.items():
            if value is not None:
                metadata[key] = value
        return metadata

    def _build_degraded_response(
        self,
        content: str,
        *,
        error_code: str,
        error_message: str,
        origin_layer: str,
        retryable: bool,
        latency_ms: float = 0.0,
        trace_id: Optional[str] = None,
        **extra: Any,
    ) -> RouterResponse:
        """Build a degraded router response with normalized metadata."""
        return RouterResponse(
            content=content,
            source="degraded",
            latency_ms=latency_ms,
            model="none",
            metadata=self._build_degraded_metadata(
                error_code=error_code,
                error_message=error_message,
                origin_layer=origin_layer,
                retryable=retryable,
                trace_id=trace_id,
                **extra,
            ),
        )

    def is_endpoint_healthy(self, endpoint_name: str = "prime") -> bool:
        """Read-only health query for ModelRouter delegation.

        Args:
            endpoint_name: Currently unused — PrimeRouter uses a single circuit
                breaker. Parameter exists for forward-compatible API when
                per-endpoint circuits are added.
        Returns:
            True if the prime endpoint circuit breaker allows execution.
        """
        return self._local_circuit.can_execute()

    async def initialize(self) -> None:
        """Initialize the router and its clients."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            logger.info("[PrimeRouter] Initializing...")

            # Initialize Prime client
            try:
                from backend.core.prime_client import get_prime_client
                self._prime_client = await get_prime_client()
                logger.info("[PrimeRouter] Prime client initialized")
            except ImportError:
                try:
                    from core.prime_client import get_prime_client
                    self._prime_client = await get_prime_client()
                    logger.info("[PrimeRouter] Prime client initialized (relative import)")
                except Exception as e:
                    logger.warning(f"[PrimeRouter] Could not initialize Prime client: {e}")
                    self._prime_client = None

            # Initialize graceful degradation
            try:
                from backend.core.graceful_degradation import get_degradation
                self._graceful_degradation = get_degradation()
                logger.info("[PrimeRouter] Graceful degradation initialized")
            except ImportError:
                try:
                    from core.graceful_degradation import get_degradation
                    self._graceful_degradation = get_degradation()
                except Exception as e:
                    logger.debug(f"[PrimeRouter] Graceful degradation not available: {e}")

            # Initialize cloud client (lazy - only if needed)
            self._cloud_client = None

            self._initialized = True
            logger.info("[PrimeRouter] Initialization complete")

    async def _get_cloud_client(self):
        """Get or create cloud Claude client (lazy initialization)."""
        if self._cloud_client is None:
            try:
                from anthropic import AsyncAnthropic
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    self._cloud_client = AsyncAnthropic(api_key=api_key)
                    logger.info("[PrimeRouter] Cloud Claude client initialized")
            except ImportError:
                logger.warning("[PrimeRouter] anthropic package not available")
        return self._cloud_client

    def _decide_route(self) -> RoutingDecision:
        """v290.0 / P0-2: GCP-first routing policy with Disease 10 boot gate.

        Decision order:
        1. [P0-2] Boot routing policy gate (while startup policy not finalised)
           — if DEGRADED, force DEGRADED immediately.
        2. GCP_PRIME (always first when promoted + circuit healthy)
        3. CLOUD_CLAUDE (paid fallback, always reliable)
        4. LOCAL_PRIME / HYBRID (last resort, only if no remote option)
        5. DEGRADED (nothing available)

        Memory emergency additionally blocks local inference to prevent
        thrash amplification.
        """
        self._guard_mirror("_decide_route")

        # P0-2: Consult Disease 10 startup routing policy while boot is in progress.
        # The policy tracks GCP handshake status and deadline; once finalized it is
        # bypassed so normal runtime routing logic takes over.
        if _g_boot_routing_policy is not None and not _g_boot_routing_policy.is_finalized:
            try:
                from backend.core.startup_routing_policy import BootRoutingDecision
                boot_decision, fallback_reason = _g_boot_routing_policy.decide()
                if boot_decision == BootRoutingDecision.DEGRADED:
                    logger.warning(
                        "[PrimeRouter] Boot policy forces DEGRADED "
                        "(fallback_reason=%s)", fallback_reason.value,
                    )
                    return RoutingDecision.DEGRADED
                # PENDING / GCP_PRIME / LOCAL_MINIMAL / CLOUD_CLAUDE → fall through
                # to normal routing so we get the most current live signal.
            except Exception:
                pass  # Never block routing on policy errors

        is_emergency = self._is_memory_emergency()

        # -- Priority 1: GCP J-Prime (always first when available) --
        if self._gcp_promoted and self._local_circuit.can_execute():
            return RoutingDecision.GCP_PRIME

        # -- Priority 2: Cloud Claude (reliable paid fallback) --
        if self._config.enable_cloud_fallback:
            return RoutingDecision.CLOUD_CLAUDE

        # -- Priority 3: Local Prime (last resort, blocked during emergency) --
        if is_emergency:
            # Local inference worsens memory thrash -- skip entirely
            return RoutingDecision.DEGRADED

        prime_available = (
            self._prime_client is not None
            and self._prime_client.is_available
        )
        local_circuit_ok = self._local_circuit.can_execute()

        if prime_available and local_circuit_ok:
            if self._config.prefer_local:
                return RoutingDecision.HYBRID
            return RoutingDecision.LOCAL_PRIME

        # -- Priority 4: Nothing available --
        return RoutingDecision.DEGRADED

    # -----------------------------------------------------------------
    # v280.4: Memory-aware routing gate
    # -----------------------------------------------------------------

    def _is_memory_emergency(self) -> bool:
        """Check if system is in EMERGENCY memory thrash.

        Uses cached result for 2 seconds to avoid repeated MemoryQuantizer
        access on the hot inference path.
        """
        now = time.monotonic()
        if now - getattr(self, "_mem_emergency_last_check", 0.0) < 2.0:
            return getattr(self, "_mem_emergency_cached", False)

        is_emergency = False
        try:
            # Check MemoryQuantizer thrash state (authoritative source)
            import backend.core.memory_quantizer as _mq_mod
            _mq = _mq_mod._memory_quantizer_instance
            if _mq is not None:
                is_emergency = getattr(_mq, "_thrash_state", "healthy") == "emergency"
        except Exception:
            pass

        if not is_emergency:
            # Fallback: check env var set by GCPHybridPrimeRouter
            is_emergency = os.environ.get("JARVIS_GCP_OFFLOAD_ACTIVE", "").lower() in (
                "1", "true",
            )

        self._mem_emergency_last_check = now
        self._mem_emergency_cached = is_emergency
        return is_emergency

    # -----------------------------------------------------------------
    # v271.0: Flapping protection
    # -----------------------------------------------------------------

    def _check_transition_cooldown(self, transition_name: str) -> bool:
        """
        v271.0: Returns True if transition is allowed (cooldown elapsed).
        Returns False if within cooldown window (flapping protection).
        """
        now = time.monotonic()
        elapsed = now - self._last_transition_time
        if self._last_transition_time > 0 and elapsed < self._transition_cooldown_s:
            logger.warning(
                "[PrimeRouter] v271.0: %s blocked by flapping protection "
                "(%.1fs < %.0fs cooldown)",
                transition_name, elapsed, self._transition_cooldown_s,
            )
            return False
        return True

    # -----------------------------------------------------------------
    # Disease 10: Mirror mode
    # -----------------------------------------------------------------

    @property
    def mirror_mode(self) -> bool:
        return self._mirror_mode

    @property
    def mirror_decisions_issued(self) -> int:
        return self._mirror_decisions_issued

    def set_mirror_mode(self, enabled: bool) -> None:
        self._mirror_mode = enabled
        if enabled:
            logger.info("[PrimeRouter] Mirror mode ENABLED — all mutations blocked")
        else:
            logger.info("[PrimeRouter] Mirror mode DISABLED")

    def _guard_mirror(self, method_name: str) -> None:
        if self._mirror_mode:
            raise MirrorModeError(
                f"PrimeRouter.{method_name}() blocked: mirror mode active"
            )

    # -----------------------------------------------------------------
    # v232.0: Late-arriving GCP VM promotion
    # -----------------------------------------------------------------

    async def promote_gcp_endpoint(self, host: str, port: int) -> bool:
        """
        v232.0: Promote PrimeRouter to use a GCP VM endpoint.

        Called when the unified supervisor detects that the Invincible Node
        (GCP VM) has become ready.  Hot-swaps the PrimeClient's endpoint
        and updates routing decisions to prefer GCP_PRIME.

        Returns True if promotion succeeded (GCP endpoint is healthy).
        """
        self._guard_mirror("promote_gcp_endpoint")
        # Phase 1: Acquire lock, check state, claim transition
        async with self._lock:
            if self._transition_in_flight:
                logger.info("[PrimeRouter] Transition already in flight, skipping promote")
                return self._gcp_promoted

            if not self._initialized:
                await self.initialize()

            if self._prime_client is None:
                logger.warning("[PrimeRouter] Cannot promote GCP endpoint: no prime client")
                return False

            # v273.0: Idempotent steady-state promotion should bypass cooldown checks.
            if (
                self._gcp_promoted
                and self._gcp_host == host
                and self._gcp_port == port
            ):
                logger.info(
                    "[PrimeRouter] v273.0: GCP endpoint already active (%s:%s) — "
                    "promotion treated as successful",
                    host,
                    port,
                )
                return True

            # v271.0: Flapping protection
            if not self._check_transition_cooldown("promote_gcp_endpoint"):
                return False

            # v272.x Phase 10: Prevent duplicate concurrent promotions
            try:
                from backend.core.idempotency_registry import check_idempotent
                if not check_idempotent("promote_gcp_endpoint", f"{host}:{port}"):
                    logger.info("[PrimeRouter] v272.x: Duplicate promotion suppressed for %s:%s", host, port)
                    return self._gcp_promoted
            except ImportError:
                pass

            # v276.0 Phase 12: Partition-aware promotion gate
            try:
                from backend.core.partition_aware_health import is_partition_detected as _is_part
                _partitioned, _part_reason = _is_part()
                if _partitioned:
                    logger.warning(
                        "[PrimeRouter] v276.0: Promotion blocked — %s", _part_reason
                    )
                    return False
            except ImportError:
                pass

            # Claim this transition — prevents concurrent promote/demote
            # while we perform the network call outside the lock.
            self._transition_in_flight = True

            # Save prior state for rollback on failure
            _prev_gcp_promoted = self._gcp_promoted
            _prev_gcp_host = self._gcp_host
            _prev_gcp_port = self._gcp_port

        # Phase 2: Network call outside lock — avoids holding lock during I/O
        logger.info(f"[PrimeRouter] v232.0: GCP VM promotion requested: {host}:{port}")
        try:
            success = await self._prime_client.update_endpoint(host, port)
        except Exception as e:
            logger.warning(f"[PrimeRouter] update_endpoint failed: {e}")
            success = False
        finally:
            # Phase 3: Re-acquire lock, commit state
            async with self._lock:
                self._transition_in_flight = False

                if success:
                    self._gcp_promoted = True
                    self._gcp_host = host
                    self._gcp_port = port
                    self._last_transition_time = time.monotonic()
                    self._local_circuit.reset_for_endpoint(
                        endpoint_id=f"gcp:{host}:{port}", health_checked=True
                    )
                else:
                    # Restore prior valid state instead of blindly clearing
                    self._gcp_promoted = _prev_gcp_promoted
                    self._gcp_host = _prev_gcp_host
                    self._gcp_port = _prev_gcp_port

        # Phase 4: Post-commit side effects (no lock needed, no state mutation)
        if success:
            try:
                _uc = await _get_ultra_coordinator()
                if _uc and _uc.cancel_shielded_task("prime_router"):
                    logger.info("[PrimeRouter] v242.0 Cancelled orphan prime_router task on GCP promotion")
            except Exception:
                pass
            try:
                from backend.core.decision_log import record_decision, DECISION_ROUTING_PROMOTE
                record_decision(
                    decision_type=DECISION_ROUTING_PROMOTE,
                    reason=f"GCP VM endpoint promoted: {host}:{port}",
                    inputs={"host": host, "port": port},
                    outcome="promoted",
                    component="prime_router",
                )
            except ImportError:
                pass
            logger.info("[PrimeRouter] v232.0: GCP VM promotion successful, routing updated")
        else:
            logger.warning("[PrimeRouter] v232.0: GCP VM promotion failed, keeping current routing")

        return success

    async def demote_gcp_endpoint(self) -> bool:
        """
        v232.0: Demote back from GCP VM to local Prime endpoint.

        Called when the GCP VM becomes unhealthy or is terminated.
        Returns True if demotion succeeded.
        """
        self._guard_mirror("demote_gcp_endpoint")
        # Phase 1: Acquire lock, check state, claim transition
        async with self._lock:
            if self._prime_client is None:
                return False

            if self._transition_in_flight:
                logger.info("[PrimeRouter] Transition already in flight, skipping demote")
                return False

            # v271.0: Flapping protection
            if not self._check_transition_cooldown("demote_gcp_endpoint"):
                return False

            _prev_host = self._gcp_host
            _prev_port = self._gcp_port
            self._transition_in_flight = True

        # Phase 2: Network call outside lock
        try:
            success = await self._prime_client.demote_to_fallback()
        except Exception as e:
            logger.warning(f"[PrimeRouter] demote_to_fallback failed: {e}")
            success = False
        finally:
            # Phase 3: Re-acquire lock, commit state
            async with self._lock:
                self._transition_in_flight = False

                if success:
                    self._gcp_promoted = False
                    self._gcp_host = None
                    self._gcp_port = None
                    self._last_transition_time = time.monotonic()
                    self._local_circuit.reset_for_endpoint(
                        endpoint_id="local", health_checked=False
                    )

        # Phase 4: Post-commit side effects
        if success:
            try:
                from backend.core.decision_log import record_decision, DECISION_ROUTING_DEMOTE
                record_decision(
                    decision_type=DECISION_ROUTING_DEMOTE,
                    reason="GCP VM endpoint demoted back to local",
                    inputs={"previous_host": _prev_host, "previous_port": _prev_port},
                    outcome="demoted",
                    component="prime_router",
                )
            except ImportError:
                pass
            logger.info("[PrimeRouter] v232.0: Demoted from GCP VM to local Prime")
        return success

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        deadline: Optional[float] = None,
        **kwargs
    ) -> RouterResponse:
        """
        v88.0: Generate a response with ultra protection stack.

        This is the main entry point for all AI inference requests.
        Now includes v88.0 protection:
        - Adaptive circuit breaker with ML-based prediction
        - Backpressure handling with AIMD rate limiting
        - W3C distributed tracing
        - Timeout enforcement

        Args:
            prompt: User prompt
            system_prompt: System prompt for the model
            context: Conversation history
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            deadline: v241.0 monotonic clock deadline (None = no deadline)
            **kwargs: Additional parameters

        Returns:
            RouterResponse with the generated content
        """
        if not self._initialized:
            await self.initialize()

        logger.info(
            f"[PrimeRouter] generate: max_tokens={max_tokens}, "
            f"temp={temperature}, prompt_len={len(prompt)}"
        )

        # v88.0: Use ultra coordinator protection if available
        ultra_coord = await _get_ultra_coordinator()
        if ultra_coord:
            # v241.0: Cap timeout to remaining deadline budget
            timeout = compute_remaining(deadline, float(os.getenv("PRIME_ROUTER_TIMEOUT", "90.0")))
            success, result, metadata = await ultra_coord.execute_with_protection(
                component="prime_router",
                operation=lambda: self._generate_internal(
                    prompt, system_prompt, context, max_tokens, temperature,
                    deadline=deadline, **kwargs
                ),
                timeout=timeout,
            )
            if success and result is not None:
                # Inject trace context into response metadata
                if "trace_id" in metadata:
                    result.metadata["v88_trace_id"] = metadata["trace_id"]
                return result
            elif not success:
                # Protection failed, return degraded response
                error_msg = metadata.get("error_message") or metadata.get("error") or "Unknown protection error"
                logger.warning(f"[PrimeRouter] v88.0 Protection failed: {error_msg}")
                return self._build_degraded_response(
                    content="I'm experiencing some difficulties. Please try again.",
                    error_code=self._classify_protection_error(metadata),
                    error_message=error_msg,
                    origin_layer=metadata.get("origin_layer", "trinity_ultra_coordinator"),
                    retryable=bool(metadata.get("retryable", True)),
                    trace_id=metadata.get("trace_id"),
                    circuit_open=metadata.get("circuit_open"),
                    failure_class=metadata.get("failure_class"),
                    reason=metadata.get("reason"),
                )

        # Fallback: direct execution without protection
        return await self._generate_internal(
            prompt, system_prompt, context, max_tokens, temperature,
            deadline=deadline, **kwargs
        )

    async def _generate_internal(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        deadline: Optional[float] = None,
        **kwargs
    ) -> RouterResponse:
        """
        v88.0: Internal generation logic (called by protection wrapper).

        Routes between local JARVIS-Prime and cloud Claude API.
        """
        start_time = time.time()
        self._metrics.total_requests += 1

        routing = self._decide_route()
        logger.debug(f"[PrimeRouter] Routing decision: {routing.value}")

        try:
            if routing == RoutingDecision.HYBRID:
                # Try local/GCP first, then cloud
                response = await self._generate_hybrid(
                    prompt, system_prompt, context, max_tokens, temperature,
                    deadline=deadline, **kwargs
                )
            elif routing == RoutingDecision.GCP_PRIME:
                # v242.0: GCP path with proper timeout + cloud fallback + circuit recording
                try:
                    _gcp_eff = compute_remaining(deadline, self._config.gcp_timeout)
                    # Gap 2: Record activity at request START so long-running generations
                    # (e.g., 60s+ codegen) don't falsely trigger idle-stop mid-stream.
                    # Idle timeout measures silence, not active generation time.
                    try:
                        from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
                        _vm_mgr_pre = await get_gcp_vm_manager_safe()
                        if _vm_mgr_pre is not None:
                            _vm_mgr_pre.record_jprime_activity()
                    except Exception:
                        pass
                    response = await asyncio.wait_for(
                        self._generate_local(
                            prompt, system_prompt, context, max_tokens, temperature, **kwargs
                        ),
                        timeout=_gcp_eff,
                    )
                    self._local_circuit.record_success()
                    # Reset J-Prime idle timer so the VM is not stopped during active sessions.
                    # Best-effort: import failure or missing singleton must never block inference.
                    try:
                        from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
                        _vm_mgr = await get_gcp_vm_manager_safe()
                        if _vm_mgr is not None:
                            _vm_mgr.record_jprime_activity()
                    except Exception:
                        pass
                except Exception as e:
                    self._local_circuit.record_failure(
                        endpoint_id=f"gcp:{self._gcp_host}:{self._gcp_port}" if self._gcp_host else None
                    )
                    logger.warning(f"[PrimeRouter] v242.0 GCP_PRIME failed, fallback to cloud: {e}")
                    if not self._config.enable_cloud_fallback:
                        raise
                    _cloud_eff = compute_remaining(deadline, self._config.cloud_timeout)
                    response = await asyncio.wait_for(
                        self._generate_cloud(
                            prompt, system_prompt, context, max_tokens, temperature, **kwargs
                        ),
                        timeout=_cloud_eff,
                    )
                    response.fallback_used = True
                    response.metadata["fallback_reason"] = str(e)
            elif routing == RoutingDecision.LOCAL_PRIME:
                response = await self._generate_local(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                )
            elif routing == RoutingDecision.CLOUD_CLAUDE:
                response = await self._generate_cloud(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                )
            else:
                response = self._generate_degraded(prompt)

            # Update metrics
            latency = (time.time() - start_time) * 1000
            response.latency_ms = latency
            self._metrics.total_latency_ms += latency

            if response.source == "local_prime":
                self._metrics.local_requests += 1
                self._metrics.local_latency_ms += latency
            elif response.source == "cloud_claude":
                self._metrics.cloud_requests += 1
                self._metrics.cloud_latency_ms += latency

            if response.fallback_used:
                self._metrics.fallback_count += 1

            return response

        except Exception as e:
            self._metrics.errors += 1
            logger.error(f"[PrimeRouter] Generation failed: {e}")

            # Return degraded response on error
            return self._build_degraded_response(
                content=f"I apologize, but I'm experiencing technical difficulties. Error: {str(e)}",
                error_code="dependency_unavailable",
                error_message=str(e),
                origin_layer="prime_router",
                retryable=True,
                latency_ms=(time.time() - start_time) * 1000,
            )

    async def _generate_hybrid(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        deadline: Optional[float] = None,
        **kwargs
    ) -> RouterResponse:
        """Try local Prime first, fall back to cloud on failure."""
        try:
            # v242.0: HYBRID is now only for local Prime (GCP goes to GCP_PRIME path).
            # Removed stale env var check that could give 120s timeout to local endpoint.
            base_timeout = self._config.local_timeout
            # v241.0: Circuit breaker probe uses short timeout; then cap to deadline
            probed_timeout = self._local_circuit.get_timeout_override(base_timeout)
            effective_timeout = compute_remaining(deadline, probed_timeout)

            response = await asyncio.wait_for(
                self._generate_local(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                ),
                timeout=effective_timeout,
            )
            self._local_circuit.record_success()
            return response
        except Exception as e:
            self._local_circuit.record_failure(
                endpoint_id=f"gcp:{self._gcp_host}:{self._gcp_port}" if self._gcp_promoted else "local"
            )
            logger.warning(f"[PrimeRouter] Local generation failed, falling back to cloud: {e}")

            if not self._config.enable_cloud_fallback:
                raise

            # v241.0: Cloud fallback also respects deadline
            effective_cloud = compute_remaining(deadline, self._config.cloud_timeout)
            response = await asyncio.wait_for(
                self._generate_cloud(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                ),
                timeout=effective_cloud,
            )
            response.fallback_used = True
            response.metadata["fallback_reason"] = str(e)
            return response

    async def _generate_local(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> RouterResponse:
        """Generate using local JARVIS-Prime."""
        if self._prime_client is None:
            raise RuntimeError("Prime client not available")

        response = await self._prime_client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            context=context,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs
        )

        # Record VM activity when routed to GCP (belt + suspenders with transport layer)
        if self._gcp_promoted or bool(os.environ.get("JARVIS_INVINCIBLE_NODE_IP")):
            try:
                from core.gcp_vm_manager import record_vm_activity
                gcp_ip = os.environ.get("JARVIS_INVINCIBLE_NODE_IP", "")
                if gcp_ip:
                    record_vm_activity(ip_address=gcp_ip)
            except Exception:
                pass  # Never break inference for metrics

        return RouterResponse(
            content=response.content,
            # v235.4: Distinguish GCP from local for metrics/logging.
            # Check env var too (handles dual-module aliasing).
            source="gcp_prime" if (self._gcp_promoted or bool(os.environ.get("JARVIS_INVINCIBLE_NODE_IP"))) else "local_prime",
            latency_ms=response.latency_ms,
            model=response.model,
            tokens_used=response.tokens_used,
            metadata=response.metadata,
        )

    async def _generate_cloud(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> RouterResponse:
        """Generate using cloud Claude API."""
        client = await self._get_cloud_client()
        if client is None:
            raise RuntimeError("Cloud client not available")

        # Build messages
        messages = []
        if context:
            messages.extend(context)
        messages.append({"role": "user", "content": prompt})

        start_time = time.time()

        # v237.0: Pass stop sequences to Anthropic API
        create_kwargs = {
            "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            "max_tokens": max_tokens,
            "system": system_prompt or "You are JARVIS, an intelligent AI assistant.",
            "messages": messages,
        }
        stop_seqs = kwargs.get("stop")
        if stop_seqs:
            create_kwargs["stop_sequences"] = stop_seqs

        # v241.0: deadline-aware cloud timeout
        _cloud_timeout = compute_remaining(
            kwargs.get("deadline"), self._config.cloud_timeout
        ) if "deadline" in kwargs else self._config.cloud_timeout
        response = await asyncio.wait_for(
            client.messages.create(**create_kwargs),
            timeout=_cloud_timeout,
        )

        latency_ms = (time.time() - start_time) * 1000
        content = response.content[0].text if response.content else ""

        return RouterResponse(
            content=content,
            source="cloud_claude",
            latency_ms=latency_ms,
            model=response.model,
            tokens_used=response.usage.output_tokens if response.usage else 0,
            metadata={"usage": response.usage.model_dump() if response.usage else {}},
        )

    def _generate_degraded(self, prompt: str) -> RouterResponse:
        """Return degraded response when no backend available."""
        return self._build_degraded_response(
            content="I apologize, but both local and cloud AI services are currently unavailable. Please try again later.",
            error_code="no_backend_available",
            error_message="No backend available",
            origin_layer="prime_router",
            retryable=True,
        )

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Generate streaming response.

        Yields content chunks as they arrive.
        """
        if not self._initialized:
            await self.initialize()

        # Try local Prime streaming first
        if self._prime_client and self._prime_client.is_available:
            try:
                async for chunk in self._prime_client.generate_stream(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    context=context,
                    **kwargs
                ):
                    yield chunk
                return
            except Exception as e:
                logger.warning(f"[PrimeRouter] Local streaming failed: {e}")

        # Fall back to cloud streaming
        if self._config.enable_cloud_fallback:
            client = await self._get_cloud_client()
            if client:
                # v237.0: Copy context to avoid mutating caller's list
                messages = list(context or [])
                messages.append({"role": "user", "content": prompt})

                # v237.0: Pass stop sequences to Anthropic streaming API
                stream_kwargs = {
                    "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
                    "max_tokens": kwargs.get("max_tokens", 4096),
                    "temperature": kwargs.get("temperature", 0.7),
                    "system": system_prompt or "You are JARVIS.",
                    "messages": messages,
                }
                stop_seqs = kwargs.get("stop")
                if stop_seqs:
                    stream_kwargs["stop_sequences"] = stop_seqs

                async with client.messages.stream(**stream_kwargs) as stream:
                    async for text in stream.text_stream:
                        yield text
                return

        # No streaming available
        yield "Streaming not available - services are offline."

    def get_status(self) -> Dict[str, Any]:
        """Get current router status."""
        return {
            "initialized": self._initialized,
            "config": {
                "prefer_local": self._config.prefer_local,
                "enable_cloud_fallback": self._config.enable_cloud_fallback,
                "local_timeout": self._config.local_timeout,
                "cloud_timeout": self._config.cloud_timeout,
            },
            "prime_client": {
                "available": self._prime_client is not None,
                "status": self._prime_client.get_status() if self._prime_client else None,
            },
            "cloud_client": {
                "available": self._cloud_client is not None,
            },
            "metrics": {
                "total_requests": self._metrics.total_requests,
                "local_requests": self._metrics.local_requests,
                "cloud_requests": self._metrics.cloud_requests,
                "fallback_count": self._metrics.fallback_count,
                "avg_latency_ms": round(self._metrics.avg_latency_ms, 2),
                "local_ratio": round(self._metrics.local_ratio, 3),
                "errors": self._metrics.errors,
            },
        }

    async def close(self) -> None:
        """Close the router and cleanup resources."""
        if self._prime_client:
            await self._prime_client.close()
        if self._cloud_client:
            await self._cloud_client.close()
        self._initialized = False
        logger.info("[PrimeRouter] Closed")


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_prime_router: Optional[PrimeRouter] = None
_router_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_prime_router(config: Optional[PrimeRouterConfig] = None) -> PrimeRouter:
    """
    Get the singleton PrimeRouter instance.

    Thread-safe with double-check locking.
    """
    global _prime_router

    if _prime_router is not None and _prime_router._initialized:
        return _prime_router

    async with _router_lock:
        if _prime_router is not None and _prime_router._initialized:
            return _prime_router

        _prime_router = PrimeRouter(config)
        await _prime_router.initialize()
        return _prime_router


async def close_prime_router() -> None:
    """Close the singleton router."""
    global _prime_router

    if _prime_router:
        await _prime_router.close()
        _prime_router = None


# -----------------------------------------------------------------
# v232.0: Module-level GCP VM promotion notifications
# -----------------------------------------------------------------

async def notify_gcp_vm_ready(host: str, port: int) -> bool:
    """
    v232.0: Notify PrimeRouter that a GCP VM is ready.

    Called by unified_supervisor when ``_propagate_invincible_node_url()``
    succeeds.  Safe to call even if PrimeRouter is not yet initialized —
    it will initialize on demand.

    Returns True if promotion succeeded.
    """
    global _prime_router

    if _prime_router is None:
        logger.info("[PrimeRouter] GCP VM ready but router not initialized yet, initializing...")
        router = await get_prime_router()
    else:
        router = _prime_router

    return await router.promote_gcp_endpoint(host, port)


async def notify_gcp_vm_unhealthy() -> bool:
    """
    v232.0: Notify PrimeRouter that the GCP VM is no longer healthy.

    Called by unified_supervisor when ``_clear_invincible_node_url()`` is
    invoked.

    Returns True if demotion succeeded.
    """
    global _prime_router

    if _prime_router is None:
        return False

    return await _prime_router.demote_gcp_endpoint()
