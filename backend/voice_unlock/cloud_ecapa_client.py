#!/usr/bin/env python3
"""
Cloud ECAPA Client - Robust Cloud Speaker Embedding Client
============================================================

Advanced async client for cloud ECAPA speaker embedding service.
Designed for high reliability with:
- Circuit breaker pattern
- Automatic retries with exponential backoff
- Dynamic endpoint discovery
- Connection pooling
- Response caching
- Comprehensive telemetry
- GCP Spot VM auto-creation with scale-to-zero
- Intelligent cost-aware backend routing

v18.2.0 - Full Hybrid Cloud with Spot VM Integration!

BACKEND OPTIONS & COSTS:
┌─────────────────┬──────────────┬─────────────────┬──────────────────────────┐
│ Backend         │ Cost/Hour    │ Cost/Month 24/7 │ Best For                 │
├─────────────────┼──────────────┼─────────────────┼──────────────────────────┤
│ Cloud Run       │ ~$0.05/hr    │ ~$5-15/month    │ Low usage, pay-per-use   │
│ Spot VM         │ $0.029/hr    │ $21/month       │ Medium use, scale-to-zero│
│ Regular VM      │ $0.268/hr    │ $195/month      │ AVOID - too expensive!   │
│ Local           │ $0.00        │ $0/month        │ High RAM available       │
└─────────────────┴──────────────┴─────────────────┴──────────────────────────┘

INTELLIGENT ROUTING PRIORITY:
1. Cloud Run (instant, serverless, pay-per-request) - DEFAULT
2. Spot VM (auto-create on demand, scale-to-zero after idle) - HIGH LOAD
3. Local fallback (if cloud unavailable and RAM > 6GB)

COST OPTIMIZATION:
- Auto-terminates idle Spot VMs after 10 minutes
- Caches embeddings to reduce API calls (60% savings)
- Routes to cheapest available backend
- Daily budget enforcement ($1/day default)

Usage:
    client = CloudECAPAClient()
    await client.initialize()

    # Extract embedding (auto-routes to best backend)
    embedding = await client.extract_embedding(audio_bytes)

    # Verify speaker
    result = await client.verify_speaker(audio_bytes, reference_embedding)

    # Get detailed cost breakdown
    costs = client.get_cost_breakdown()
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# BACKEND TYPES
# =============================================================================

class BackendType(Enum):
    """Available backend types for ECAPA processing."""
    CLOUD_RUN = "cloud_run"      # GCP Cloud Run (serverless)
    SPOT_VM = "spot_vm"          # GCP Spot VM (auto-created)
    REGULAR_VM = "regular_vm"   # GCP Regular VM (expensive, avoid)
    LOCAL = "local"              # Local ECAPA (RAM permitting)
    CACHED = "cached"            # From cache (no backend hit)


@dataclass
class BackendCostInfo:
    """Cost information for a backend."""
    backend_type: BackendType
    cost_per_hour: float
    cost_per_request: float  # Estimated per-request cost
    min_latency_ms: float
    max_latency_ms: float
    availability: float  # 0-1, higher is better

    @property
    def cost_efficiency_score(self) -> float:
        """Higher is better (lower cost, lower latency, higher availability)."""
        # Normalize: lower cost = higher score, lower latency = higher score
        cost_score = max(0, 100 - (self.cost_per_hour * 100))
        latency_score = max(0, 100 - (self.min_latency_ms / 10))
        avail_score = self.availability * 100
        return (cost_score * 0.4 + latency_score * 0.3 + avail_score * 0.3)


# Backend cost table (from GCP pricing)
BACKEND_COSTS = {
    BackendType.CLOUD_RUN: BackendCostInfo(
        backend_type=BackendType.CLOUD_RUN,
        cost_per_hour=0.05,
        cost_per_request=0.0001,  # ~$0.10 per 1000 requests
        min_latency_ms=100,
        max_latency_ms=500,
        availability=0.99,
    ),
    BackendType.SPOT_VM: BackendCostInfo(
        backend_type=BackendType.SPOT_VM,
        cost_per_hour=0.029,
        cost_per_request=0.00001,  # Amortized over requests
        min_latency_ms=50,
        max_latency_ms=200,
        availability=0.95,  # Slightly lower (can be preempted)
    ),
    BackendType.REGULAR_VM: BackendCostInfo(
        backend_type=BackendType.REGULAR_VM,
        cost_per_hour=0.268,
        cost_per_request=0.0001,
        min_latency_ms=50,
        max_latency_ms=150,
        availability=0.999,
    ),
    BackendType.LOCAL: BackendCostInfo(
        backend_type=BackendType.LOCAL,
        cost_per_hour=0.0,
        cost_per_request=0.0,
        min_latency_ms=200,
        max_latency_ms=1000,
        availability=0.8,  # Depends on RAM
    ),
    BackendType.CACHED: BackendCostInfo(
        backend_type=BackendType.CACHED,
        cost_per_hour=0.0,
        cost_per_request=0.0,
        min_latency_ms=1,
        max_latency_ms=10,
        availability=1.0,
    ),
}

# =============================================================================
# COST TRACKER
# =============================================================================

@dataclass
class CostTracker:
    """Tracks costs per backend and daily totals."""

    # Per-backend costs
    costs_by_backend: Dict[BackendType, float] = field(default_factory=dict)
    requests_by_backend: Dict[BackendType, int] = field(default_factory=dict)

    # Daily tracking
    daily_cost: float = 0.0
    daily_budget: float = 1.0  # $1/day default
    cost_reset_time: float = field(default_factory=time.time)

    # Session tracking
    session_start: float = field(default_factory=time.time)
    total_cost: float = 0.0
    total_requests: int = 0
    total_cache_hits: int = 0

    def record_request(
        self,
        backend: BackendType,
        latency_ms: float,
        from_cache: bool = False
    ):
        """Record a request with its cost."""
        if from_cache:
            self.total_cache_hits += 1
            return

        self.total_requests += 1

        # Get cost info
        cost_info = BACKEND_COSTS.get(backend)
        if cost_info:
            cost = cost_info.cost_per_request
            self.costs_by_backend[backend] = self.costs_by_backend.get(backend, 0) + cost
            self.requests_by_backend[backend] = self.requests_by_backend.get(backend, 0) + 1
            self.daily_cost += cost
            self.total_cost += cost

    def check_daily_reset(self):
        """Reset daily cost if new day."""
        now = time.time()
        # Reset after 24 hours
        if now - self.cost_reset_time > 86400:
            self.daily_cost = 0.0
            self.cost_reset_time = now

    def is_over_budget(self) -> bool:
        """Check if daily budget exceeded."""
        self.check_daily_reset()
        return self.daily_cost >= self.daily_budget

    def get_savings_from_cache(self) -> float:
        """Calculate money saved by caching."""
        # Assume each cached request would have been Cloud Run
        cloud_run_cost = BACKEND_COSTS[BackendType.CLOUD_RUN].cost_per_request
        return self.total_cache_hits * cloud_run_cost

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_duration_hours": (time.time() - self.session_start) / 3600,
            "total_cost": f"${self.total_cost:.6f}",
            "daily_cost": f"${self.daily_cost:.6f}",
            "daily_budget": f"${self.daily_budget:.2f}",
            "budget_remaining": f"${max(0, self.daily_budget - self.daily_cost):.4f}",
            "total_requests": self.total_requests,
            "cache_hits": self.total_cache_hits,
            "cache_savings": f"${self.get_savings_from_cache():.6f}",
            "costs_by_backend": {
                bt.value: f"${cost:.6f}"
                for bt, cost in self.costs_by_backend.items()
            },
            "requests_by_backend": {
                bt.value: count
                for bt, count in self.requests_by_backend.items()
            },
        }


# =============================================================================
# CONFIGURATION
# =============================================================================

class CloudECAPAClientConfig:
    """Dynamic configuration from environment."""

    # Primary endpoints (in priority order)
    ENDPOINTS = [
        e.strip() for e in
        os.getenv("JARVIS_CLOUD_ML_ENDPOINTS", "").split(",")
        if e.strip()
    ]

    # Fallback endpoint construction
    # Note: Cloud Run URLs use project NUMBER, not project ID
    GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "jarvis-473803")
    # Cloud Run URLs use format: https://{service}-{random_suffix}.a.run.app
    # The actual URL is discovered at deployment time and stored in env var
    # Default is the current deployed service URL
    # Dynamic endpoint discovery - tries multiple known endpoints
    # Updated 2024-12 to new Cloud Run URL format
    PRIMARY_ENDPOINT = os.getenv(
        "JARVIS_CLOUD_ML_ENDPOINT",
        "https://jarvis-ml-888774109345.us-central1.run.app"
    )

    # Fallback endpoints for redundancy (no hardcoding - all from env or discovery)
    FALLBACK_ENDPOINTS = [
        e.strip() for e in
        os.getenv("JARVIS_CLOUD_ML_FALLBACK_ENDPOINTS", "").split(",")
        if e.strip()
    ]

    # Timeouts
    CONNECT_TIMEOUT = float(os.getenv("CLOUD_ECAPA_CONNECT_TIMEOUT", "5.0"))
    REQUEST_TIMEOUT = float(os.getenv("CLOUD_ECAPA_REQUEST_TIMEOUT", "30.0"))

    # ECAPA model initialization wait settings
    # Cloud Run cold starts can take 30-120s for ECAPA model loading
    ECAPA_WAIT_FOR_READY = os.getenv("CLOUD_ECAPA_WAIT_FOR_READY", "true").lower() == "true"
    ECAPA_READY_TIMEOUT = float(os.getenv("CLOUD_ECAPA_READY_TIMEOUT", "120.0"))  # Max wait time
    ECAPA_READY_POLL_INTERVAL = float(os.getenv("CLOUD_ECAPA_READY_POLL", "5.0"))  # Poll every 5s

    # Retries
    MAX_RETRIES = int(os.getenv("CLOUD_ECAPA_MAX_RETRIES", "3"))
    RETRY_BACKOFF_BASE = float(os.getenv("CLOUD_ECAPA_BACKOFF_BASE", "1.0"))
    RETRY_BACKOFF_MAX = float(os.getenv("CLOUD_ECAPA_BACKOFF_MAX", "10.0"))

    # Circuit breaker
    CB_FAILURE_THRESHOLD = int(os.getenv("CLOUD_ECAPA_CB_FAILURES", "5"))
    CB_RECOVERY_TIMEOUT = float(os.getenv("CLOUD_ECAPA_CB_RECOVERY", "30.0"))
    CB_SUCCESS_THRESHOLD = int(os.getenv("CLOUD_ECAPA_CB_SUCCESS", "2"))

    # Caching
    CACHE_ENABLED = os.getenv("CLOUD_ECAPA_CACHE_ENABLED", "true").lower() == "true"
    CACHE_TTL = int(os.getenv("CLOUD_ECAPA_CACHE_TTL", "300"))  # 5 minutes
    CACHE_MAX_SIZE = int(os.getenv("CLOUD_ECAPA_CACHE_SIZE", "100"))

    # Health check
    HEALTH_CHECK_INTERVAL = float(os.getenv("CLOUD_ECAPA_HEALTH_INTERVAL", "60.0"))
    HEALTH_CHECK_TIMEOUT = float(os.getenv("CLOUD_ECAPA_HEALTH_TIMEOUT", "5.0"))

    @classmethod
    def get_all_endpoints(cls) -> List[str]:
        """Get all configured endpoints in priority order."""
        endpoints = []

        # Add explicitly configured endpoints
        if cls.ENDPOINTS:
            endpoints.extend(cls.ENDPOINTS)

        # Add primary endpoint if not in list
        if cls.PRIMARY_ENDPOINT and cls.PRIMARY_ENDPOINT not in endpoints:
            endpoints.append(cls.PRIMARY_ENDPOINT)

        # Add Cloud Run default - uses correct Cloud Run URL format
        # Note: Cloud Run URLs are https://{service}-{random_suffix}.a.run.app
        # The actual URL is discovered at deployment time
        cloud_run_default = f"{cls.PRIMARY_ENDPOINT}/api/ml" if cls.PRIMARY_ENDPOINT else None
        if cloud_run_default and cloud_run_default not in endpoints:
            endpoints.append(cloud_run_default)

        # Add localhost for development
        if os.getenv("JARVIS_DEV_MODE", "false").lower() == "true":
            localhost = "http://localhost:8010/api/ml"
            if localhost not in endpoints:
                endpoints.insert(0, localhost)  # Prefer local in dev mode

        return endpoints

    # =========================================================================
    # SPOT VM CONFIGURATION
    # =========================================================================

    # Enable Spot VM backend
    SPOT_VM_ENABLED = os.getenv("JARVIS_SPOT_VM_ENABLED", "true").lower() == "true"

    # Spot VM creation triggers
    SPOT_VM_TRIGGER_FAILURES = int(os.getenv("JARVIS_SPOT_VM_TRIGGER_FAILURES", "3"))
    SPOT_VM_TRIGGER_LATENCY_MS = float(os.getenv("JARVIS_SPOT_VM_TRIGGER_LATENCY_MS", "2000"))

    # Spot VM idle timeout (auto-terminate)
    SPOT_VM_IDLE_TIMEOUT_MIN = float(os.getenv("JARVIS_SPOT_VM_IDLE_TIMEOUT", "10"))

    # Daily budget for Spot VMs
    SPOT_VM_DAILY_BUDGET = float(os.getenv("JARVIS_SPOT_VM_DAILY_BUDGET", "1.0"))

    # RAM thresholds for backend selection
    RAM_THRESHOLD_LOCAL_GB = float(os.getenv("JARVIS_RAM_THRESHOLD_LOCAL", "6.0"))
    RAM_THRESHOLD_CLOUD_GB = float(os.getenv("JARVIS_RAM_THRESHOLD_CLOUD", "4.0"))
    RAM_THRESHOLD_CRITICAL_GB = float(os.getenv("JARVIS_RAM_THRESHOLD_CRITICAL", "2.0"))

    # Routing preferences
    PREFER_CLOUD_RUN = os.getenv("JARVIS_PREFER_CLOUD_RUN", "true").lower() == "true"
    CLOUD_FALLBACK_ENABLED = os.getenv("JARVIS_CLOUD_FALLBACK", "true").lower() == "true"


# =============================================================================
# SPOT VM BACKEND
# =============================================================================

class SpotVMBackend:
    """
    Manages GCP Spot VM for ECAPA processing.

    Integrates with GCPVMManager for:
    - Auto-creation when Cloud Run is unavailable/slow
    - Scale-to-zero (auto-terminate after idle)
    - Cost tracking
    """

    def __init__(self):
        self._vm_manager = None
        self._active_vm = None
        self._vm_endpoint: Optional[str] = None
        self._initialized = False
        self._creating = False
        self._last_activity = time.time()

        # Stats
        self._stats = {
            "vm_creations": 0,
            "vm_terminations": 0,
            "requests_handled": 0,
            "total_cost": 0.0,
        }

    async def initialize(self) -> bool:
        """Initialize Spot VM backend."""
        if self._initialized:
            return True

        if not CloudECAPAClientConfig.SPOT_VM_ENABLED:
            logger.info("ℹ️  Spot VM backend disabled by configuration")
            return False

        try:
            # Import GCP VM Manager (lazy import to avoid circular deps)
            from core.gcp_vm_manager import get_gcp_vm_manager, COMPUTE_AVAILABLE

            if not COMPUTE_AVAILABLE:
                logger.info(
                    "ℹ️  Spot VM backend unavailable (google-cloud-compute not installed). "
                    "Using Cloud Run only. Install with: pip install google-cloud-compute"
                )
                return False

            self._vm_manager = await get_gcp_vm_manager()
            self._initialized = True
            logger.info("✅ Spot VM backend initialized")
            return True
        except ImportError as e:
            logger.info(
                f"ℹ️  Spot VM backend unavailable: {e}. "
                "Cloud Run will be used as primary backend."
            )
            return False
        except RuntimeError as e:
            # This is expected when google-cloud-compute isn't installed
            if "not installed" in str(e).lower():
                logger.info(
                    "ℹ️  Spot VM backend unavailable (GCP Compute API not configured). "
                    "Using Cloud Run as primary backend."
                )
            else:
                logger.warning(f"⚠️  Spot VM backend initialization error: {e}")
            return False
        except Exception as e:
            logger.warning(f"⚠️  Spot VM backend initialization failed: {e}")
            return False

    async def ensure_vm_available(self) -> bool:
        """Ensure a Spot VM is available for ECAPA processing."""
        if not self._initialized:
            if not await self.initialize():
                return False

        # Check if we already have an active VM
        if self._active_vm and self._vm_endpoint:
            # Verify it's still running
            try:
                await self._health_check()
                return True
            except Exception:
                logger.warning("Active Spot VM unhealthy, will create new one")
                self._active_vm = None
                self._vm_endpoint = None

        # Create a new Spot VM if needed
        if not self._creating:
            return await self._create_vm()

        return False

    async def _create_vm(self) -> bool:
        """Create a new Spot VM for ECAPA."""
        if not self._vm_manager:
            return False

        self._creating = True
        logger.info("🚀 Creating Spot VM for ECAPA processing...")

        try:
            # Get memory snapshot for VM creation decision
            from core.platform_memory_monitor import get_memory_snapshot
            memory_snapshot = await get_memory_snapshot()

            # Create the VM
            vm = await self._vm_manager.create_vm(
                components=["ecapa-tdnn", "speaker-embedding"],
                trigger_reason="Cloud ECAPA client fallback",
                metadata={"client": "cloud_ecapa_client", "version": "18.2.0"}
            )

            if vm:
                self._active_vm = vm
                # Construct endpoint URL from VM IP
                if vm.ip_address:
                    self._vm_endpoint = f"http://{vm.ip_address}:8010/api/ml"
                elif vm.internal_ip:
                    self._vm_endpoint = f"http://{vm.internal_ip}:8010/api/ml"

                self._stats["vm_creations"] += 1
                logger.info(f"✅ Spot VM created: {vm.name} ({self._vm_endpoint})")
                return True
            else:
                logger.error("Failed to create Spot VM")
                return False

        except Exception as e:
            logger.error(f"Error creating Spot VM: {e}")
            return False
        finally:
            self._creating = False

    async def _health_check(self) -> bool:
        """Check if the Spot VM is healthy."""
        if not self._vm_endpoint:
            return False

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._vm_endpoint}/health",
                    timeout=5
                ) as response:
                    return response.status == 200
        except Exception:
            return False

    def get_endpoint(self) -> Optional[str]:
        """Get the Spot VM endpoint if available."""
        return self._vm_endpoint

    async def record_activity(self):
        """Record activity to prevent idle timeout."""
        self._last_activity = time.time()
        self._stats["requests_handled"] += 1

        if self._active_vm:
            self._active_vm.record_activity()

    async def check_idle_timeout(self) -> bool:
        """Check if VM should be terminated due to idle timeout."""
        if not self._active_vm:
            return False

        idle_minutes = (time.time() - self._last_activity) / 60

        if idle_minutes >= CloudECAPAClientConfig.SPOT_VM_IDLE_TIMEOUT_MIN:
            logger.info(
                f"⏰ Spot VM idle for {idle_minutes:.1f}m "
                f"(threshold: {CloudECAPAClientConfig.SPOT_VM_IDLE_TIMEOUT_MIN}m)"
            )
            await self.terminate_vm("Idle timeout")
            return True

        return False

    async def terminate_vm(self, reason: str = "Manual"):
        """Terminate the active Spot VM."""
        if not self._active_vm or not self._vm_manager:
            return

        logger.info(f"🛑 Terminating Spot VM: {reason}")

        try:
            # Update cost before termination
            self._active_vm.update_cost()
            self._stats["total_cost"] += self._active_vm.total_cost

            await self._vm_manager.terminate_vm(
                self._active_vm.name,
                reason=reason
            )
            self._stats["vm_terminations"] += 1

        except Exception as e:
            logger.error(f"Error terminating Spot VM: {e}")
        finally:
            self._active_vm = None
            self._vm_endpoint = None

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "active_vm": self._active_vm.name if self._active_vm else None,
            "endpoint": self._vm_endpoint,
            "idle_minutes": (time.time() - self._last_activity) / 60,
        }


# Global Spot VM backend instance
_spot_vm_backend: Optional[SpotVMBackend] = None


async def get_spot_vm_backend() -> SpotVMBackend:
    """Get or create Spot VM backend singleton."""
    global _spot_vm_backend
    if _spot_vm_backend is None:
        _spot_vm_backend = SpotVMBackend()
    return _spot_vm_backend


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing recovery


@dataclass
class EndpointCircuitBreaker:
    """Per-endpoint circuit breaker."""

    endpoint: str
    failure_threshold: int = CloudECAPAClientConfig.CB_FAILURE_THRESHOLD
    recovery_timeout: float = CloudECAPAClientConfig.CB_RECOVERY_TIMEOUT
    success_threshold: int = CloudECAPAClientConfig.CB_SUCCESS_THRESHOLD

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    half_open_success: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    last_error: Optional[str] = None

    # Stats
    total_requests: int = 0
    total_failures: int = 0
    total_successes: int = 0

    def record_success(self):
        """Record successful request."""
        self.total_requests += 1
        self.total_successes += 1
        self.success_count += 1
        self.failure_count = 0
        self.last_success_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.half_open_success += 1
            if self.half_open_success >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.half_open_success = 0
                logger.info(f"[CircuitBreaker] {self.endpoint}: CLOSED (recovered)")

    def record_failure(self, error: str = None):
        """Record failed request."""
        self.total_requests += 1
        self.total_failures += 1
        self.failure_count += 1
        self.success_count = 0
        self.last_failure_time = time.time()
        self.last_error = error

        if self.state == CircuitState.HALF_OPEN:
            # Failure in half-open → back to open
            self.state = CircuitState.OPEN
            self.half_open_success = 0
            logger.warning(f"[CircuitBreaker] {self.endpoint}: OPEN (half-open failure)")
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker] {self.endpoint}: OPEN ({self.failure_count} failures)")

    def can_execute(self) -> Tuple[bool, str]:
        """Check if request can proceed."""
        if self.state == CircuitState.CLOSED:
            return True, "closed"

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_success = 0
                    logger.info(f"[CircuitBreaker] {self.endpoint}: HALF_OPEN (testing)")
                    return True, "half_open"
            return False, f"open (last error: {self.last_error})"

        # HALF_OPEN: allow request
        return True, "half_open"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "state": self.state.name,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_error": self.last_error,
            "total_requests": self.total_requests,
            "success_rate": f"{(self.total_successes / max(1, self.total_requests)) * 100:.1f}%",
        }


# =============================================================================
# RESPONSE CACHE
# =============================================================================

@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    value: Any
    timestamp: float
    ttl: float

    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


class EmbeddingCache:
    """LRU cache for embeddings with TTL."""

    def __init__(self, max_size: int = 100, ttl: int = 300):
        self.max_size = max_size
        self.default_ttl = ttl
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

        # Stats
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """Get item from cache."""
        async with self._lock:
            if key not in self.cache:
                self.misses += 1
                return None

            entry = self.cache[key]
            if entry.is_expired():
                del self.cache[key]
                self.misses += 1
                return None

            # Move to end (LRU)
            self.cache.move_to_end(key)
            self.hits += 1
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store item in cache."""
        async with self._lock:
            if len(self.cache) >= self.max_size:
                # Remove oldest
                self.cache.popitem(last=False)

            self.cache[key] = CacheEntry(
                value=value,
                timestamp=time.time(),
                ttl=ttl or self.default_ttl
            )

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hit_rate * 100:.1f}%",
        }


# =============================================================================
# CLOUD ECAPA CLIENT
# =============================================================================

class CloudECAPAClient:
    """
    Robust async client for cloud ECAPA speaker embedding service.

    Features:
    - Multiple endpoint support with automatic failover
    - Per-endpoint circuit breakers
    - Retry with exponential backoff
    - Response caching
    - Connection pooling
    - Comprehensive telemetry
    - GCP Spot VM integration with scale-to-zero
    - Intelligent cost-aware backend routing
    - v19.0.0: Pre-warm endpoint support for fast cold starts

    v19.0.0 Enhancements:
    - Pre-warm API to trigger model initialization before first request
    - Manifest-based instant cache verification in Cloud Run
    - Parallel warmup patterns
    """

    VERSION = "19.0.0"

    def __init__(self):
        self._endpoints: List[str] = []
        self._circuit_breakers: Dict[str, EndpointCircuitBreaker] = {}
        self._session = None
        self._initialized = False
        self._healthy_endpoint: Optional[str] = None
        self._active_backend: BackendType = BackendType.CLOUD_RUN

        # Spot VM backend
        self._spot_vm_backend: Optional[SpotVMBackend] = None

        # Cost tracking
        self._cost_tracker = CostTracker(
            daily_budget=CloudECAPAClientConfig.SPOT_VM_DAILY_BUDGET
        )

        # Caching
        self._cache = EmbeddingCache(
            max_size=CloudECAPAClientConfig.CACHE_MAX_SIZE,
            ttl=CloudECAPAClientConfig.CACHE_TTL
        ) if CloudECAPAClientConfig.CACHE_ENABLED else None

        # Stats
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "cache_hits": 0,
            "retries": 0,
            "failovers": 0,
            "avg_latency_ms": 0.0,
            "cloud_run_failures": 0,
        }

        # Backend routing state
        self._consecutive_failures = 0
        self._last_latency_ms = 0.0

        # Background tasks
        self._health_check_task: Optional[asyncio.Task] = None
        self._idle_check_task: Optional[asyncio.Task] = None

    async def initialize(self) -> bool:
        """
        Initialize the client with endpoint discovery.

        Returns:
            True if at least one endpoint is available
        """
        if self._initialized:
            return True

        logger.info("=" * 60)
        logger.info("Initializing Cloud ECAPA Client")
        logger.info("=" * 60)

        # Discover endpoints
        self._endpoints = CloudECAPAClientConfig.get_all_endpoints()

        if not self._endpoints:
            logger.error("No cloud ECAPA endpoints configured!")
            return False

        logger.info(f"Configured {len(self._endpoints)} endpoints:")
        for i, ep in enumerate(self._endpoints):
            logger.info(f"  {i + 1}. {ep}")

        # Initialize circuit breakers for each endpoint
        for endpoint in self._endpoints:
            self._circuit_breakers[endpoint] = EndpointCircuitBreaker(endpoint=endpoint)

        # Create robust aiohttp session with enhanced connection handling
        try:
            import aiohttp
            import ssl

            timeout = aiohttp.ClientTimeout(
                total=CloudECAPAClientConfig.REQUEST_TIMEOUT,
                connect=CloudECAPAClientConfig.CONNECT_TIMEOUT,
                sock_read=CloudECAPAClientConfig.REQUEST_TIMEOUT,
                sock_connect=CloudECAPAClientConfig.CONNECT_TIMEOUT,
            )

            # Create SSL context for Cloud Run HTTPS
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED

            # Enhanced connection pooling with keepalive and auto-reconnect
            connector = aiohttp.TCPConnector(
                limit=30,  # Increased max connections
                limit_per_host=10,  # Increased per-host limit
                ttl_dns_cache=300,  # DNS cache TTL
                ssl=ssl_context,  # Proper SSL handling
                keepalive_timeout=30,  # Keep connections alive
                enable_cleanup_closed=True,  # Clean up closed connections
                force_close=False,  # Allow connection reuse
            )

            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "User-Agent": f"JARVIS-CloudECAPA/{self.VERSION}",
                    "Accept": "application/json",
                    "Connection": "keep-alive",
                },
            )

            # Store session creation time for potential refresh
            self._session_created_at = time.time()
            self._session_max_age = 3600  # Refresh session every hour

        except ImportError:
            logger.error("aiohttp not available. Install with: pip install aiohttp")
            return False

        # Verify at least one endpoint is healthy (with wait-for-ready for cold starts)
        logger.info("🔍 Checking Cloud Run endpoint availability...")
        healthy = await self._discover_healthy_endpoint(wait_for_ready=True)

        if healthy:
            self._initialized = True
            self._active_backend = BackendType.CLOUD_RUN
            logger.info(f"✅ Cloud ECAPA Client ready (primary: {self._healthy_endpoint})")

            # Start background health monitoring
            self._health_check_task = asyncio.create_task(self._health_check_loop())

            # Start idle check for Spot VMs
            self._idle_check_task = asyncio.create_task(self._idle_check_loop())

            # Initialize Spot VM backend in background (non-blocking, optional fallback)
            asyncio.create_task(self._init_spot_vm_background())

            return True

        # No Cloud Run endpoints available - try Spot VM if enabled
        logger.info("⚠️  Cloud Run endpoints not available, checking fallback options...")

        if CloudECAPAClientConfig.SPOT_VM_ENABLED:
            logger.info("🔄 Trying Spot VM backend...")
            self._spot_vm_backend = await get_spot_vm_backend()
            if self._spot_vm_backend and await self._spot_vm_backend.initialize():
                if await self._spot_vm_backend.ensure_vm_available():
                    self._active_backend = BackendType.SPOT_VM
                    self._initialized = True
                    logger.info("✅ Cloud ECAPA Client ready (Spot VM backend)")
                    return True

        # Check if local fallback is possible
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / (1024**3)
            if available_gb >= CloudECAPAClientConfig.RAM_THRESHOLD_LOCAL_GB:
                logger.info(
                    f"✅ Cloud ECAPA Client ready (local fallback available, "
                    f"{available_gb:.1f}GB RAM free)"
                )
                self._active_backend = BackendType.LOCAL
                self._initialized = True

                # Start background task to periodically check for Cloud Run availability
                self._health_check_task = asyncio.create_task(self._health_check_loop())
                return True
        except Exception:
            pass

        logger.warning(
            "⚠️  No healthy backends found. Client initialized for retry. "
            "Requests will attempt Cloud Run with wait-for-ready or local fallback."
        )
        self._initialized = True

        # Start health check loop to detect when endpoints become available
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        return False

    async def _init_spot_vm_background(self):
        """Initialize Spot VM backend in background (non-blocking)."""
        try:
            self._spot_vm_backend = await get_spot_vm_backend()
            await self._spot_vm_backend.initialize()
        except Exception as e:
            logger.debug(f"Background Spot VM initialization: {e}")

    async def _idle_check_loop(self):
        """Background task to check for idle Spot VMs."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute

                if self._spot_vm_backend:
                    await self._spot_vm_backend.check_idle_timeout()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Idle check loop error: {e}")

    async def _select_backend(self) -> Tuple[BackendType, Optional[str]]:
        """
        Intelligently select the best backend for the next request.

        Selection criteria:
        1. Cache (if enabled and hit)
        2. Cloud Run (if healthy)
        3. Spot VM (if Cloud Run failing or slow)
        4. Local (if enough RAM and cloud unavailable)

        Returns:
            Tuple of (BackendType, endpoint_url or None)
        """
        # Check if we're over daily budget
        if self._cost_tracker.is_over_budget():
            logger.warning("Daily budget exceeded, using local fallback only")
            return BackendType.LOCAL, None

        # Check Cloud Run health
        if self._healthy_endpoint:
            cb = self._circuit_breakers.get(self._healthy_endpoint)
            if cb and cb.state == CircuitState.CLOSED:
                # Cloud Run healthy
                return BackendType.CLOUD_RUN, self._healthy_endpoint

        # Check if we should trigger Spot VM creation
        should_create_spot = (
            CloudECAPAClientConfig.SPOT_VM_ENABLED and
            (
                self._consecutive_failures >= CloudECAPAClientConfig.SPOT_VM_TRIGGER_FAILURES or
                self._last_latency_ms >= CloudECAPAClientConfig.SPOT_VM_TRIGGER_LATENCY_MS
            )
        )

        if should_create_spot:
            # Try Spot VM
            if not self._spot_vm_backend:
                self._spot_vm_backend = await get_spot_vm_backend()

            if await self._spot_vm_backend.ensure_vm_available():
                endpoint = self._spot_vm_backend.get_endpoint()
                if endpoint:
                    return BackendType.SPOT_VM, endpoint

        # Try any available Cloud Run endpoint
        for endpoint in self._endpoints:
            cb = self._circuit_breakers.get(endpoint)
            can_execute, _ = cb.can_execute() if cb else (True, "")
            if can_execute:
                return BackendType.CLOUD_RUN, endpoint

        # Check local RAM availability
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / (1024**3)
            if available_gb >= CloudECAPAClientConfig.RAM_THRESHOLD_LOCAL_GB:
                return BackendType.LOCAL, None
        except Exception:
            pass

        # Fallback to any endpoint (will probably fail)
        if self._endpoints:
            return BackendType.CLOUD_RUN, self._endpoints[0]

        return BackendType.LOCAL, None

    async def _discover_healthy_endpoint(self, wait_for_ready: bool = True) -> bool:
        """
        Find a healthy endpoint with extraction test.

        Args:
            wait_for_ready: Whether to wait for ECAPA model to become ready
                           (handles Cloud Run cold start)

        Returns:
            True if a healthy endpoint was found
        """
        should_wait = wait_for_ready and CloudECAPAClientConfig.ECAPA_WAIT_FOR_READY

        for endpoint in self._endpoints:
            try:
                # First check: quick health check without waiting
                # to see if any endpoint is already ready
                healthy = await self._check_endpoint_health(
                    endpoint,
                    test_extraction=False,
                    wait_for_ready=False
                )
                if healthy:
                    self._healthy_endpoint = endpoint
                    logger.info(f"✅ Found ready endpoint: {endpoint}")
                    return True
            except Exception as e:
                logger.debug(f"Quick check for {endpoint} failed: {e}")

        # No endpoint immediately ready - try waiting for first one if configured
        if should_wait:
            logger.info("⏳ No endpoints immediately ready, waiting for ECAPA initialization...")
            for endpoint in self._endpoints:
                try:
                    healthy = await self._check_endpoint_health(
                        endpoint,
                        test_extraction=False,
                        wait_for_ready=True
                    )
                    if healthy:
                        self._healthy_endpoint = endpoint
                        return True
                except Exception as e:
                    logger.warning(f"Endpoint {endpoint} unhealthy after waiting: {e}")

        return False

    async def _check_endpoint_health(
        self,
        endpoint: str,
        test_extraction: bool = False,
        wait_for_ready: bool = False
    ) -> bool:
        """
        Check if an endpoint is healthy.

        Args:
            endpoint: The endpoint URL to check
            test_extraction: Whether to test actual extraction
            wait_for_ready: Whether to wait for ECAPA model to become ready

        Returns:
            True if endpoint is healthy and ECAPA is ready
        """
        if not self._session:
            return False

        health_url = f"{endpoint.rstrip('/')}/health"
        start_time = time.time()
        max_wait = CloudECAPAClientConfig.ECAPA_READY_TIMEOUT if wait_for_ready else 0
        poll_interval = CloudECAPAClientConfig.ECAPA_READY_POLL_INTERVAL

        while True:
            try:
                async with self._session.get(
                    health_url,
                    timeout=CloudECAPAClientConfig.HEALTH_CHECK_TIMEOUT
                ) as response:
                    if response.status != 200:
                        logger.debug(f"Endpoint {endpoint} returned HTTP {response.status}")
                        return False

                    data = await response.json()
                    status = data.get("status", "unknown")
                    ecapa_ready = data.get("ecapa_ready", True)

                    if ecapa_ready:
                        # ECAPA is ready!
                        if wait_for_ready and time.time() - start_time > 0.1:
                            elapsed = time.time() - start_time
                            logger.info(f"✅ ECAPA model ready on {endpoint} (waited {elapsed:.1f}s)")

                        # Optional: test actual extraction
                        if test_extraction:
                            test_audio = np.zeros(1600, dtype=np.float32)  # 100ms silence
                            embedding = await self._extract_from_endpoint(
                                endpoint,
                                test_audio.tobytes(),
                                sample_rate=16000
                            )
                            if embedding is None:
                                return False

                        return True

                    # ECAPA not ready yet
                    if not wait_for_ready:
                        logger.info(f"Endpoint {endpoint} reachable but ECAPA not ready (status: {status})")
                        return False

                    # Wait and retry
                    elapsed = time.time() - start_time
                    if elapsed >= max_wait:
                        logger.warning(
                            f"⏰ Timeout waiting for ECAPA on {endpoint} "
                            f"(waited {elapsed:.1f}s, status: {status})"
                        )
                        return False

                    remaining = max_wait - elapsed
                    logger.info(
                        f"⏳ ECAPA initializing on {endpoint} (status: {status}, "
                        f"waited {elapsed:.1f}s, max {remaining:.0f}s remaining)..."
                    )
                    await asyncio.sleep(poll_interval)

            except asyncio.TimeoutError:
                if wait_for_ready and (time.time() - start_time) < max_wait:
                    logger.debug(f"Health check timeout for {endpoint}, retrying...")
                    await asyncio.sleep(poll_interval)
                    continue
                return False
            except Exception as e:
                logger.debug(f"Health check failed for {endpoint}: {e}")
                if wait_for_ready and (time.time() - start_time) < max_wait:
                    await asyncio.sleep(poll_interval)
                    continue
                return False

    async def _health_check_loop(self):
        """Background task to monitor endpoint health."""
        while True:
            try:
                await asyncio.sleep(CloudECAPAClientConfig.HEALTH_CHECK_INTERVAL)

                # Re-check all endpoints
                for endpoint in self._endpoints:
                    cb = self._circuit_breakers[endpoint]

                    # Only check endpoints that are open (potentially recovered)
                    if cb.state == CircuitState.OPEN:
                        healthy = await self._check_endpoint_health(endpoint)
                        if healthy:
                            # Allow circuit breaker to try again
                            cb.state = CircuitState.HALF_OPEN
                            logger.info(f"[HealthCheck] {endpoint}: may be recovered, testing...")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check loop error: {e}")

    async def close(self):
        """Close the client and cleanup resources."""
        logger.info("Closing Cloud ECAPA Client...")

        # Cancel background tasks
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        if self._idle_check_task:
            self._idle_check_task.cancel()
            try:
                await self._idle_check_task
            except asyncio.CancelledError:
                pass

        # Terminate any active Spot VMs
        if self._spot_vm_backend:
            await self._spot_vm_backend.terminate_vm("Client shutdown")

        # Close HTTP session
        if self._session:
            await self._session.close()
            self._session = None

        # Log cost summary
        cost_summary = self._cost_tracker.to_dict()
        logger.info("=" * 60)
        logger.info("💰 CLOUD ECAPA CLIENT COST SUMMARY")
        logger.info("=" * 60)
        logger.info(f"   Session Duration: {cost_summary['session_duration_hours']:.2f} hours")
        logger.info(f"   Total Requests:   {cost_summary['total_requests']}")
        logger.info(f"   Cache Hits:       {cost_summary['cache_hits']}")
        logger.info(f"   Total Cost:       {cost_summary['total_cost']}")
        logger.info(f"   Cache Savings:    {cost_summary['cache_savings']}")
        logger.info("=" * 60)

        self._initialized = False
        logger.info("✅ Cloud ECAPA Client closed")

    # =========================================================================
    # MAIN API
    # =========================================================================

    async def prewarm(self, endpoint: str = None) -> Dict[str, Any]:
        """
        Pre-warm the cloud ECAPA model for faster subsequent requests.

        v19.0.0 Enhancement:
        - Calls /api/ml/prewarm endpoint to trigger model initialization
        - Returns detailed timing and diagnostics
        - Useful for triggering cold start before actual requests

        Args:
            endpoint: Specific endpoint to pre-warm (uses healthy endpoint if None)

        Returns:
            Pre-warm result with timing information
        """
        if not self._initialized:
            if not await self.initialize():
                return {"success": False, "error": "Client not initialized"}

        target_endpoint = endpoint or self._healthy_endpoint
        if not target_endpoint:
            return {"success": False, "error": "No healthy endpoint available"}

        # Construct pre-warm URL
        endpoint_stripped = target_endpoint.rstrip('/')
        if endpoint_stripped.endswith('/api/ml'):
            url = f"{endpoint_stripped}/prewarm"
        else:
            url = f"{endpoint_stripped}/api/ml/prewarm"

        try:
            async with self._session.post(url, json={}) as response:
                if response.status == 200:
                    result = await response.json()
                    logger.info(
                        f"Pre-warm completed: {result.get('total_time_ms', 0):.0f}ms "
                        f"(init: {result.get('initialization_time_ms', 0):.0f}ms, "
                        f"warmup: {result.get('warmup_time_ms', 0):.0f}ms)"
                    )
                    return result
                else:
                    error_text = await response.text()
                    return {
                        "success": False,
                        "error": f"HTTP {response.status}: {error_text}"
                    }

        except Exception as e:
            logger.error(f"Pre-warm failed: {e}")
            return {"success": False, "error": str(e)}

    async def extract_embedding(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
        format: str = "float32",
        use_cache: bool = True
    ) -> Optional[np.ndarray]:
        """
        Extract speaker embedding from audio with intelligent backend routing.

        Args:
            audio_data: Raw audio bytes
            sample_rate: Audio sample rate
            format: Audio format (float32, int16)
            use_cache: Whether to check cache

        Returns:
            192-dimensional speaker embedding or None
        """
        if not self._initialized:
            if not await self.initialize():
                logger.error("Client initialization failed")
                return None

        self._stats["total_requests"] += 1

        # Check cache first
        cache_key = None
        if use_cache and self._cache:
            cache_key = self._compute_cache_key(audio_data)
            cached = await self._cache.get(cache_key)
            if cached is not None:
                self._stats["cache_hits"] += 1
                self._cost_tracker.record_request(BackendType.CACHED, 0, from_cache=True)
                logger.debug("Cache hit for embedding")
                return cached

        # Select best backend
        start_time = time.time()
        backend_type, endpoint = await self._select_backend()

        logger.debug(f"Selected backend: {backend_type.value} (endpoint: {endpoint})")

        # Try extraction based on backend type
        embedding = None
        last_error = None

        if backend_type == BackendType.LOCAL:
            # Local extraction
            embedding = await self._extract_local(audio_data, sample_rate)
            if embedding is not None:
                self._consecutive_failures = 0
        else:
            # Cloud extraction (Cloud Run or Spot VM)
            try:
                embedding = await self._extract_from_endpoint(
                    endpoint,
                    audio_data,
                    sample_rate,
                    format
                )

                if embedding is not None:
                    self._consecutive_failures = 0

                    # Record success with circuit breaker
                    if endpoint in self._circuit_breakers:
                        self._circuit_breakers[endpoint].record_success()

                    # Update healthy endpoint for Cloud Run
                    if backend_type == BackendType.CLOUD_RUN:
                        self._healthy_endpoint = endpoint

                    # Record activity for Spot VM
                    if backend_type == BackendType.SPOT_VM and self._spot_vm_backend:
                        await self._spot_vm_backend.record_activity()

            except Exception as e:
                last_error = str(e)
                self._consecutive_failures += 1

                # Record failure with circuit breaker
                if endpoint in self._circuit_breakers:
                    self._circuit_breakers[endpoint].record_failure(last_error)

                if backend_type == BackendType.CLOUD_RUN:
                    self._stats["cloud_run_failures"] += 1

                logger.warning(f"Backend {backend_type.value} failed: {e}")

                # Check if endpoint might just be initializing (cold start)
                # and wait for it to become ready
                if backend_type == BackendType.CLOUD_RUN and endpoint:
                    logger.info("🔄 Checking if Cloud Run is still initializing...")
                    became_ready = await self._check_endpoint_health(
                        endpoint,
                        test_extraction=False,
                        wait_for_ready=True
                    )
                    if became_ready:
                        # Retry extraction now that endpoint is ready
                        try:
                            embedding = await self._extract_from_endpoint(
                                endpoint, audio_data, sample_rate, format
                            )
                            if embedding is not None:
                                self._consecutive_failures = 0
                                self._healthy_endpoint = endpoint
                                if endpoint in self._circuit_breakers:
                                    self._circuit_breakers[endpoint].record_success()
                                logger.info("✅ Extraction succeeded after waiting for ECAPA")
                        except Exception as retry_error:
                            logger.warning(f"Retry after wait failed: {retry_error}")

                # Fallback: try other backends if still no embedding
                if embedding is None:
                    embedding = await self._fallback_extraction(
                        audio_data, sample_rate, format,
                        exclude_backend=backend_type
                    )

        # Update stats
        latency_ms = (time.time() - start_time) * 1000
        self._last_latency_ms = latency_ms

        if embedding is not None:
            self._update_latency(latency_ms)
            self._stats["successful_requests"] += 1
            self._cost_tracker.record_request(backend_type, latency_ms)

            # Cache result
            if use_cache and self._cache and cache_key:
                await self._cache.set(cache_key, embedding)

            logger.debug(
                f"Extracted embedding via {backend_type.value} "
                f"({latency_ms:.1f}ms, shape: {embedding.shape})"
            )
            return embedding

        self._stats["failed_requests"] += 1
        logger.error(f"All backends failed. Last error: {last_error}")
        return None

    async def _fallback_extraction(
        self,
        audio_data: bytes,
        sample_rate: int,
        format: str,
        exclude_backend: BackendType
    ) -> Optional[np.ndarray]:
        """Try fallback backends after primary failure."""
        logger.info(f"Trying fallback backends (excluding {exclude_backend.value})...")

        # Try Spot VM if we haven't already
        if (
            exclude_backend != BackendType.SPOT_VM and
            CloudECAPAClientConfig.SPOT_VM_ENABLED
        ):
            if not self._spot_vm_backend:
                self._spot_vm_backend = await get_spot_vm_backend()

            if await self._spot_vm_backend.ensure_vm_available():
                endpoint = self._spot_vm_backend.get_endpoint()
                if endpoint:
                    try:
                        embedding = await self._extract_from_endpoint(
                            endpoint, audio_data, sample_rate, format
                        )
                        if embedding is not None:
                            self._stats["failovers"] += 1
                            await self._spot_vm_backend.record_activity()
                            return embedding
                    except Exception as e:
                        logger.warning(f"Spot VM fallback failed: {e}")

        # Try other Cloud Run endpoints
        if exclude_backend != BackendType.CLOUD_RUN:
            for endpoint in self._endpoints:
                if endpoint == self._healthy_endpoint:
                    continue  # Skip already-tried endpoint

                cb = self._circuit_breakers.get(endpoint)
                can_execute, _ = cb.can_execute() if cb else (True, "")

                if can_execute:
                    try:
                        embedding = await self._extract_from_endpoint(
                            endpoint, audio_data, sample_rate, format
                        )
                        if embedding is not None:
                            self._stats["failovers"] += 1
                            if cb:
                                cb.record_success()
                            return embedding
                    except Exception as e:
                        if cb:
                            cb.record_failure(str(e))
                        logger.warning(f"Fallback endpoint {endpoint} failed: {e}")

        # Try local as last resort
        if exclude_backend != BackendType.LOCAL:
            embedding = await self._extract_local(audio_data, sample_rate)
            if embedding is not None:
                self._stats["failovers"] += 1
                return embedding

        return None

    async def _extract_local(
        self,
        audio_data: bytes,
        sample_rate: int
    ) -> Optional[np.ndarray]:
        """Extract embedding using local ECAPA model."""
        try:
            # Check RAM availability
            import psutil
            available_gb = psutil.virtual_memory().available / (1024**3)

            if available_gb < CloudECAPAClientConfig.RAM_THRESHOLD_LOCAL_GB:
                logger.warning(
                    f"Insufficient RAM for local ECAPA: "
                    f"{available_gb:.1f}GB available, "
                    f"{CloudECAPAClientConfig.RAM_THRESHOLD_LOCAL_GB}GB required"
                )
                return None

            # Try ML Registry
            from voice_unlock.ml_engine_registry import get_ml_registry
            registry = await get_ml_registry()

            if await registry.ensure_ecapa_available():
                # Convert bytes to numpy
                audio_array = np.frombuffer(audio_data, dtype=np.float32)
                embedding = await registry.extract_embedding(audio_array, sample_rate)
                if embedding is not None:
                    logger.debug("Local ECAPA extraction successful")
                    return embedding

        except Exception as e:
            logger.warning(f"Local ECAPA extraction failed: {e}")

        return None

    async def verify_speaker(
        self,
        audio_data: bytes,
        reference_embedding: np.ndarray,
        sample_rate: int = 16000,
        format: str = "float32",
        threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        Verify speaker against reference embedding.

        Args:
            audio_data: Raw audio bytes
            reference_embedding: Reference speaker embedding
            sample_rate: Audio sample rate
            format: Audio format
            threshold: Verification threshold

        Returns:
            Verification result dict
        """
        # Extract embedding from audio
        embedding = await self.extract_embedding(
            audio_data,
            sample_rate,
            format,
            use_cache=True
        )

        if embedding is None:
            return {
                "success": False,
                "verified": False,
                "error": "Failed to extract embedding"
            }

        # Compute similarity
        similarity = self._compute_similarity(embedding, reference_embedding)
        confidence = (similarity + 1) / 2  # Normalize to 0-1

        return {
            "success": True,
            "verified": confidence >= threshold,
            "similarity": float(similarity),
            "confidence": float(confidence),
            "threshold": threshold,
        }

    async def _refresh_session_if_stale(self):
        """Refresh HTTP session if it's too old or has connection issues."""
        import aiohttp
        import ssl

        # Check if session needs refresh (older than max age)
        if hasattr(self, '_session_created_at'):
            session_age = time.time() - self._session_created_at
            if session_age > getattr(self, '_session_max_age', 3600):
                logger.info("🔄 Refreshing stale HTTP session...")
                await self._recreate_session()

    async def _recreate_session(self):
        """Recreate the aiohttp session (fixes connection pool issues)."""
        import aiohttp
        import ssl

        # Close old session if it exists
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass

        timeout = aiohttp.ClientTimeout(
            total=CloudECAPAClientConfig.REQUEST_TIMEOUT,
            connect=CloudECAPAClientConfig.CONNECT_TIMEOUT,
            sock_read=CloudECAPAClientConfig.REQUEST_TIMEOUT,
            sock_connect=CloudECAPAClientConfig.CONNECT_TIMEOUT,
        )

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED

        connector = aiohttp.TCPConnector(
            limit=30,
            limit_per_host=10,
            ttl_dns_cache=300,
            ssl=ssl_context,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
            force_close=False,
        )

        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={
                "User-Agent": f"JARVIS-CloudECAPA/{self.VERSION}",
                "Accept": "application/json",
                "Connection": "keep-alive",
            },
        )

        self._session_created_at = time.time()
        logger.info("✅ HTTP session recreated successfully")

    async def _extract_from_endpoint(
        self,
        endpoint: str,
        audio_data: bytes,
        sample_rate: int = 16000,
        format: str = "float32"
    ) -> Optional[np.ndarray]:
        """Extract embedding from a specific endpoint with robust error handling."""
        import aiohttp

        if not self._session:
            await self._recreate_session()

        # Check for stale session
        await self._refresh_session_if_stale()

        # Construct the correct URL based on endpoint format
        # Cloud Run service has /api/ml/speaker_embedding as the extraction endpoint
        endpoint_stripped = endpoint.rstrip('/')
        if endpoint_stripped.endswith('/api/ml'):
            url = f"{endpoint_stripped}/speaker_embedding"
        else:
            url = f"{endpoint_stripped}/api/ml/speaker_embedding"

        audio_b64 = base64.b64encode(audio_data).decode('utf-8')

        payload = {
            "audio_data": audio_b64,
            "sample_rate": sample_rate,
            "format": format,
        }

        # Retry with exponential backoff and connection error handling
        last_error = None
        for attempt in range(1, CloudECAPAClientConfig.MAX_RETRIES + 1):
            try:
                async with self._session.post(url, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()

                        if result.get("success") and result.get("embedding"):
                            embedding = np.array(result["embedding"], dtype=np.float32)
                            logger.debug(f"Extracted embedding from {endpoint}: shape {embedding.shape}")
                            return embedding

                        error = result.get("error", "Unknown error")
                        raise RuntimeError(f"Extraction failed: {error}")

                    elif response.status >= 500:
                        # Server error - retry
                        raise RuntimeError(f"Server error: HTTP {response.status}")
                    else:
                        # Client error - don't retry
                        error_text = await response.text()
                        raise ValueError(f"HTTP {response.status}: {error_text}")

            except aiohttp.ServerDisconnectedError as e:
                # Server disconnected - recreate session and retry
                logger.warning(f"Server disconnected (attempt {attempt}), recreating session...")
                await self._recreate_session()
                last_error = e
                if attempt < CloudECAPAClientConfig.MAX_RETRIES:
                    await asyncio.sleep(1.0)  # Brief pause before retry
                    self._stats["retries"] += 1
                    continue

            except aiohttp.ClientConnectorError as e:
                # Connection error - retry with backoff
                logger.warning(f"Connection error (attempt {attempt}): {e}")
                last_error = e
                if attempt < CloudECAPAClientConfig.MAX_RETRIES:
                    backoff = min(
                        CloudECAPAClientConfig.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
                        CloudECAPAClientConfig.RETRY_BACKOFF_MAX
                    )
                    await asyncio.sleep(backoff)
                    self._stats["retries"] += 1
                    continue

            except asyncio.TimeoutError as e:
                # Timeout - don't retry immediately, might be overloaded
                logger.warning(f"Request timeout (attempt {attempt})")
                last_error = e
                if attempt < CloudECAPAClientConfig.MAX_RETRIES:
                    await asyncio.sleep(2.0)  # Longer pause for timeout
                    self._stats["retries"] += 1
                    continue

            except ValueError as e:
                # Client error - don't retry
                raise
            except Exception as e:
                if attempt < CloudECAPAClientConfig.MAX_RETRIES:
                    backoff = min(
                        CloudECAPAClientConfig.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
                        CloudECAPAClientConfig.RETRY_BACKOFF_MAX
                    )
                    logger.debug(f"Retry {attempt} after {backoff}s: {e}")
                    await asyncio.sleep(backoff)
                    self._stats["retries"] += 1
                else:
                    raise

        return None

    def _get_ordered_endpoints(self) -> List[str]:
        """Get endpoints ordered by health (healthy first)."""
        def endpoint_priority(ep: str) -> int:
            cb = self._circuit_breakers.get(ep)
            if not cb:
                return 100

            if ep == self._healthy_endpoint:
                return 0  # Primary healthy endpoint first
            elif cb.state == CircuitState.CLOSED:
                return 1
            elif cb.state == CircuitState.HALF_OPEN:
                return 2
            else:  # OPEN
                return 3

        return sorted(self._endpoints, key=endpoint_priority)

    def _compute_cache_key(self, audio_data: bytes) -> str:
        """Compute cache key for audio."""
        return hashlib.sha256(audio_data).hexdigest()[:16]

    def _compute_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        """Compute cosine similarity."""
        norm1 = np.linalg.norm(emb1)
        norm2 = np.linalg.norm(emb2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(emb1, emb2) / (norm1 * norm2))

    def _update_latency(self, latency_ms: float):
        """Update average latency stat."""
        # Note: This is called BEFORE successful_requests is incremented,
        # so we need to handle the case where n would be 0
        n = self._stats["successful_requests"]
        old_avg = self._stats["avg_latency_ms"]

        if n == 0:
            # First request - just set the latency directly
            self._stats["avg_latency_ms"] = latency_ms
        else:
            # Running average: new_avg = (old_avg * n + new_value) / (n + 1)
            self._stats["avg_latency_ms"] = (old_avg * n + latency_ms) / (n + 1)

    def get_stats(self) -> Dict[str, Any]:
        """Get client statistics."""
        return {
            **self._stats,
            "version": self.VERSION,
            "initialized": self._initialized,
            "active_backend": self._active_backend.value,
            "healthy_endpoint": self._healthy_endpoint,
            "endpoints": len(self._endpoints),
            "consecutive_failures": self._consecutive_failures,
            "last_latency_ms": self._last_latency_ms,
            "circuit_breakers": {
                ep: cb.to_dict()
                for ep, cb in self._circuit_breakers.items()
            },
            "cache": self._cache.stats() if self._cache else None,
            "cost": self._cost_tracker.to_dict(),
            "spot_vm": self._spot_vm_backend.get_stats() if self._spot_vm_backend else None,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get detailed client status."""
        return {
            "version": self.VERSION,
            "ready": self._initialized and (
                self._healthy_endpoint is not None or
                (self._spot_vm_backend and self._spot_vm_backend.get_endpoint())
            ),
            "active_backend": self._active_backend.value,
            "healthy_endpoint": self._healthy_endpoint,
            "spot_vm_endpoint": self._spot_vm_backend.get_endpoint() if self._spot_vm_backend else None,
            "all_endpoints": self._endpoints,
            "circuit_breakers": {
                ep: {
                    "state": cb.state.name,
                    "failures": cb.failure_count,
                    "last_error": cb.last_error,
                }
                for ep, cb in self._circuit_breakers.items()
            },
            "stats": self.get_stats(),
        }

    def get_cost_breakdown(self) -> Dict[str, Any]:
        """Get detailed cost breakdown by backend."""
        return {
            "version": self.VERSION,
            "summary": self._cost_tracker.to_dict(),
            "backend_costs": {
                bt.value: {
                    "cost_per_hour": f"${info.cost_per_hour:.4f}",
                    "cost_per_request": f"${info.cost_per_request:.6f}",
                    "min_latency_ms": info.min_latency_ms,
                    "max_latency_ms": info.max_latency_ms,
                    "efficiency_score": f"{info.cost_efficiency_score:.1f}",
                }
                for bt, info in BACKEND_COSTS.items()
            },
            "routing_status": {
                "consecutive_failures": self._consecutive_failures,
                "last_latency_ms": self._last_latency_ms,
                "spot_vm_trigger_threshold": CloudECAPAClientConfig.SPOT_VM_TRIGGER_FAILURES,
                "latency_trigger_threshold_ms": CloudECAPAClientConfig.SPOT_VM_TRIGGER_LATENCY_MS,
            },
            "spot_vm": self._spot_vm_backend.get_stats() if self._spot_vm_backend else None,
        }


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_client_instance: Optional[CloudECAPAClient] = None
_client_lock = asyncio.Lock()


async def get_cloud_ecapa_client() -> CloudECAPAClient:
    """Get or create the global cloud ECAPA client."""
    global _client_instance

    async with _client_lock:
        if _client_instance is None:
            _client_instance = CloudECAPAClient()
            await _client_instance.initialize()

        return _client_instance


async def close_cloud_ecapa_client():
    """Close the global client."""
    global _client_instance

    if _client_instance:
        await _client_instance.close()
        _client_instance = None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def extract_embedding_cloud(
    audio_data: bytes,
    sample_rate: int = 16000,
    format: str = "float32"
) -> Optional[np.ndarray]:
    """
    Convenience function to extract speaker embedding via cloud.

    Args:
        audio_data: Raw audio bytes
        sample_rate: Audio sample rate
        format: Audio format

    Returns:
        Speaker embedding or None
    """
    client = await get_cloud_ecapa_client()
    return await client.extract_embedding(audio_data, sample_rate, format)


async def verify_speaker_cloud(
    audio_data: bytes,
    reference_embedding: np.ndarray,
    sample_rate: int = 16000,
    threshold: float = 0.85
) -> Dict[str, Any]:
    """
    Convenience function to verify speaker via cloud.

    Args:
        audio_data: Raw audio bytes
        reference_embedding: Reference speaker embedding
        sample_rate: Audio sample rate
        threshold: Verification threshold

    Returns:
        Verification result
    """
    client = await get_cloud_ecapa_client()
    return await client.verify_speaker(
        audio_data,
        reference_embedding,
        sample_rate,
        threshold=threshold
    )
