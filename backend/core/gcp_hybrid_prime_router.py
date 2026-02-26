"""
GCP Hybrid Prime Router v153.0 (Enterprise Graceful Degradation)
================================================================

Intelligent routing between local JARVIS Prime, GCP VMs, and cloud APIs
with unified cost management across all repos.

v153.0 ENTERPRISE FEATURE: Graceful Degradation & Recovery Cascade
- Multi-tier fallback: GCP VM → Cloud Run → Cloud API → Degraded Local
- GCP failure isolation: Provisioning failures don't cascade to system failure
- Recovery escalation: Automatic retry with exponential backoff + jitter
- Context-aware cooldown: Distinguishes OOM vs transient vs config failures
- Cross-repo coordination: Signals recovery state to JARVIS/Prime/Reactor

v93.0 MAJOR FEATURE: Predictive Memory Defense
- Adaptive polling: 1s checks when RAM > 60% (vs 5s normal)
- Rate-of-change (derivative) trigger: 100MB/sec growth triggers GCP instantly
- Emergency offload: SIGSTOP/SIGCONT for local LLM processes at 80% RAM
- Lowered thresholds: 70% trigger (was 85%) for M1 Mac memory compression

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
    │   │ RAM > 8GB    │   │ RAM > 70%│   │ Fallback   │              │
    │   └──────────────┘   └──────────┘   └────────────┘              │
    │                                                                  │
    │   v93.0 Predictive Memory Defense:                              │
    │   ┌─────────────────────────────────────────────────────────┐   │
    │   │  60% RAM → Fast polling (1s)                            │   │
    │   │  70% RAM → Trigger GCP provisioning                     │   │
    │   │  80% RAM → EMERGENCY: SIGSTOP local LLMs                │   │
    │   │  100MB/s → SPIKE: Instant GCP trigger (any RAM level)   │   │
    │   └─────────────────────────────────────────────────────────┘   │
    │                                                                  │
    │   Integration Points:                                            │
    │   - CrossRepoCostSync: Unified budget tracking                  │
    │   - CrossRepoNeuralMesh: Prime/Reactor coordination             │
    │   - TrinityBridgeAdapter: Model update notifications            │
    │   - ProcessIsolatedMLLoader: LLM subprocess tracking            │
    └─────────────────────────────────────────────────────────────────┘

Routing Logic:
1. Check unified budget - if exceeded, force local-only mode
2. Check local RAM - if sufficient, use local Prime (free)
3. Check GCP VM availability - if available and budget allows, use GCP
4. Fallback to Cloud Claude API if all else fails

v93.0 Changes (Predictive Memory Defense):
- Lowered GCP_TRIGGER_RAM_PERCENT from 85% to 70% for M1 Mac
- Lowered VM_PROVISIONING_THRESHOLD from 80% to 70%
- Added adaptive polling: 1s at >60% RAM, 5s otherwise
- Added rate-of-change (derivative) trigger for memory spikes
- Added emergency offload with SIGSTOP/SIGCONT for local LLM processes
- Added memory history tracking for spike detection
- Enhanced metrics with predictive defense state

v2.0 Changes:
- Replaced inline circuit breaker with CrossRepoCircuitBreaker
- Added failure classification for intelligent retry decisions
- Added CorrelationContext for distributed tracing
- Added detailed execution metrics and latency tracking
- Improved error handling with categorized failures

Author: Trinity System
Version: 153.0.0
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

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
# Enterprise Hooks Integration (v148.1)
# =============================================================================

ENTERPRISE_HOOKS_AVAILABLE = False
try:
    from backend.core.enterprise_hooks import (
        enterprise_init,
        handle_memory_pressure,
        handle_gcp_failure,
        GCPErrorContext,
        record_provider_success,
        record_provider_failure,
        update_component_health,
        is_enterprise_available,
        RecoveryStrategy,
    )
    from backend.core.health_contracts import HealthStatus
    ENTERPRISE_HOOKS_AVAILABLE = True
    logger.info("[v148.1] Enterprise hooks available - intelligent recovery enabled")
except ImportError as e:
    logger.debug(f"[v148.1] Enterprise hooks not available: {e}")
    enterprise_init = None
    handle_memory_pressure = None
    handle_gcp_failure = None
    GCPErrorContext = None
    record_provider_success = None
    record_provider_failure = None
    update_component_health = None
    is_enterprise_available = lambda: False
    RecoveryStrategy = None
    HealthStatus = None

# =============================================================================
# Configuration
# =============================================================================

# RAM thresholds (in GB)
LOCAL_PRIME_MIN_RAM_GB = float(os.getenv("LOCAL_PRIME_MIN_RAM_GB", "8.0"))
# v93.0: Lowered from 85.0 to 70.0 for M1 Mac memory compression awareness
GCP_TRIGGER_RAM_PERCENT = float(os.getenv("GCP_TRIGGER_RAM_PERCENT", "70.0"))
# v93.0: Lowered from 95.0 to 85.0 - 95% is OOM territory on M1
CRITICAL_RAM_PERCENT = float(os.getenv("CRITICAL_RAM_PERCENT", "85.0"))
# v93.0: Lowered from 80.0 to 70.0 for proactive VM provisioning
VM_PROVISIONING_THRESHOLD = float(os.getenv("VM_PROVISIONING_THRESHOLD", "70.0"))

# v93.0: Predictive Memory Defense Configuration
# Adaptive polling - faster checks when memory pressure detected
MEMORY_ADAPTIVE_POLL_THRESHOLD = float(os.getenv("MEMORY_ADAPTIVE_POLL_THRESHOLD", "60.0"))  # Start fast polling at 60%
MEMORY_FAST_POLL_INTERVAL = float(os.getenv("MEMORY_FAST_POLL_INTERVAL", "1.0"))  # 1s when RAM > 60%
MEMORY_NORMAL_POLL_INTERVAL = float(os.getenv("MEMORY_NORMAL_POLL_INTERVAL", "5.0"))  # 5s otherwise

# Rate-of-change (derivative) trigger - detect memory spikes
MEMORY_SPIKE_RATE_THRESHOLD_MB = float(os.getenv("MEMORY_SPIKE_RATE_THRESHOLD_MB", "100.0"))  # 100MB/sec = immediate trigger
MEMORY_DERIVATIVE_WINDOW_SEC = float(os.getenv("MEMORY_DERIVATIVE_WINDOW_SEC", "3.0"))  # Calculate rate over 3s window

# v266.1: Emergency offload configuration — clean unload primary, SIGSTOP last resort
# Step 1 threshold: clean model unload via COMPONENT_UNLOAD
EMERGENCY_UNLOAD_RAM_PERCENT = float(os.getenv("EMERGENCY_UNLOAD_RAM_PERCENT", "85.0"))  # Clean unload at 85%
# Post-unload verification: wait this long to check if memory dropped
EMERGENCY_UNLOAD_VERIFY_DELAY_SEC = float(os.getenv("EMERGENCY_UNLOAD_VERIFY_DELAY_SEC", "12.0"))
# Minimum RAM drop (GB) to consider unload successful
EMERGENCY_UNLOAD_MIN_DROP_GB = float(os.getenv("EMERGENCY_UNLOAD_MIN_DROP_GB", "1.0"))
# Step 3 threshold: SIGSTOP as last resort (one-shot, no cycling)
EMERGENCY_SIGSTOP_RAM_PERCENT = float(os.getenv("EMERGENCY_SIGSTOP_RAM_PERCENT", "95.0"))
EMERGENCY_SIGSTOP_TIMEOUT_SEC = float(os.getenv("EMERGENCY_SIGSTOP_TIMEOUT_SEC", "60.0"))
# Cooldown applies to SIGSTOP only (clean unload has no cycling problem)
EMERGENCY_SIGSTOP_COOLDOWN_SEC = float(os.getenv("EMERGENCY_SIGSTOP_COOLDOWN_SEC", "120.0"))
# Hysteresis: RAM must drop this much below unload trigger before re-enabling
EMERGENCY_OFFLOAD_HYSTERESIS = float(os.getenv("EMERGENCY_OFFLOAD_HYSTERESIS", "10.0"))
# v266.2: Post-crisis model recovery — hybrid lazy + background warm-up
# After emergency offload, reload local model when memory stabilizes
RECOVERY_STABILITY_THRESHOLD_PERCENT = float(os.getenv("EMERGENCY_RECOVERY_THRESHOLD_PERCENT", "75.0"))
RECOVERY_STABILITY_DURATION_SEC = float(os.getenv("EMERGENCY_RECOVERY_STABILITY_SEC", "30.0"))
RECOVERY_MAX_ATTEMPTS = int(os.getenv("EMERGENCY_RECOVERY_MAX_ATTEMPTS", "3"))
RECOVERY_STABILITY_MIN_SEC = float(os.getenv("EMERGENCY_RECOVERY_STABILITY_MIN_SEC", "30.0"))
RECOVERY_STABILITY_MAX_SEC = float(os.getenv("EMERGENCY_RECOVERY_STABILITY_MAX_SEC", "60.0"))
RECOVERY_COOLDOWN_SEC = float(os.getenv("EMERGENCY_RECOVERY_COOLDOWN_SEC", "300.0"))
RECOVERY_TREND_RISE_EPSILON = float(os.getenv("EMERGENCY_RECOVERY_TREND_RISE_EPSILON", "0.25"))
RECOVERY_BACKOFF_SCHEDULE_SEC = [30.0, 60.0, 120.0]

# Cross-repo signaling for memory pressure
from pathlib import Path
CROSS_REPO_DIR = Path.home() / ".jarvis" / "cross_repo"
MEMORY_PRESSURE_SIGNAL_FILE = CROSS_REPO_DIR / "memory_pressure.json"

# Cost thresholds
MAX_SINGLE_REQUEST_COST = float(os.getenv("MAX_SINGLE_REQUEST_COST", "0.50"))
PREFER_LOCAL_BELOW_COST = float(os.getenv("PREFER_LOCAL_BELOW_COST", "0.10"))

# VM provisioning (v2.0)
VM_PROVISIONING_ENABLED = os.getenv("GCP_VM_PROVISIONING_ENABLED", "true").lower() == "true"
VM_PROVISIONING_LOCK_TTL = int(os.getenv("VM_PROVISIONING_LOCK_TTL", "300"))  # 5 minutes
VM_MIN_ACTIVE_REQUESTS = int(os.getenv("VM_MIN_ACTIVE_REQUESTS", "1"))  # Min requests before termination

# v266.0: Pressure-driven VM lifecycle hysteresis
GCP_RELEASE_RAM_PERCENT = float(os.getenv("GCP_RELEASE_RAM_PERCENT", "70.0"))
GCP_TRIGGER_READINGS_REQUIRED = int(os.getenv("GCP_TRIGGER_READINGS_REQUIRED", "3"))
GCP_TRIGGER_READINGS_WINDOW = int(os.getenv("GCP_TRIGGER_READINGS_WINDOW", "5"))
GCP_ACTIVE_STABILITY_CHECKS = int(os.getenv("GCP_ACTIVE_STABILITY_CHECKS", "3"))
GCP_COOLING_GRACE_SECONDS = float(os.getenv("GCP_COOLING_GRACE_SECONDS", "120.0"))

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


class VMLifecycleState(Enum):
    """v266.0: State machine for pressure-driven GCP VM lifecycle."""
    IDLE = "idle"
    TRIGGERING = "triggering"
    PROVISIONING = "provisioning"
    BOOTING = "booting"
    ACTIVE = "active"
    COOLING_DOWN = "cooling_down"
    STOPPING = "stopping"


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
# v153.0: ENTERPRISE GRACEFUL DEGRADATION & RECOVERY CASCADE
# =============================================================================
# This system ensures that GCP provisioning failures don't cascade into
# system-wide failures. Instead, we gracefully degrade through multiple tiers
# and provide intelligent recovery with context-aware cooldowns.
# =============================================================================

class GCPFailureType(Enum):
    """
    v153.0: Classification of GCP failures for intelligent recovery.

    Different failure types require different recovery strategies:
    - QUOTA: Wait for quota refresh, use alternative regions
    - CREDENTIALS: Requires manual intervention, skip GCP entirely
    - NETWORK: Transient, retry with backoff
    - SPOT_UNAVAILABLE: Try on-demand or different zone
    - TIMEOUT: Retry with longer timeout
    - CONFIG: Permanent until fixed, skip GCP
    - OOM_RECOVERY: Memory pressure triggered, use cloud fallback
    """
    QUOTA = "quota"                  # GCP quota exceeded
    CREDENTIALS = "credentials"       # Auth failure
    NETWORK = "network"              # Network/connectivity issue
    SPOT_UNAVAILABLE = "spot_unavailable"  # No spot VMs available
    TIMEOUT = "timeout"              # Provisioning timeout
    CONFIG = "config"                # Misconfiguration
    OOM_RECOVERY = "oom_recovery"    # Triggered by OOM
    UNKNOWN = "unknown"              # Unclassified failure


@dataclass
class RecoveryState:
    """
    v153.0: Tracks recovery state for a specific failure scenario.
    """
    failure_type: GCPFailureType
    failure_count: int = 0
    last_failure_time: float = 0.0
    cooldown_until: float = 0.0
    recovery_attempts: int = 0
    last_recovery_time: float = 0.0
    permanently_disabled: bool = False
    disable_reason: Optional[str] = None

    def is_in_cooldown(self) -> bool:
        """Check if we're still in cooldown period."""
        return time.time() < self.cooldown_until

    def calculate_backoff(self, base: float = 30.0, max_backoff: float = 600.0) -> float:
        """Calculate exponential backoff with jitter."""
        import random
        backoff = min(base * (2 ** self.failure_count), max_backoff)
        # Add 10-30% jitter to prevent thundering herd
        jitter = backoff * (0.1 + random.random() * 0.2)
        return backoff + jitter


class RecoveryCascadeManager:
    """
    v153.0: Manages multi-tier fallback and intelligent recovery.

    When GCP provisioning fails, this manager:
    1. Classifies the failure type
    2. Determines appropriate cooldown based on failure type
    3. Selects the next best fallback tier
    4. Signals recovery state to cross-repo components
    5. Attempts recovery when cooldown expires

    Fallback cascade order:
    1. LOCAL_PRIME (if RAM sufficient)
    2. GCP_VM (if provisioning enabled)
    3. GCP_CLOUD_RUN (if configured)
    4. CLOUD_CLAUDE (API fallback)
    5. DEGRADED_LOCAL (minimal functionality)
    """

    # Shared cross-repo state file for recovery coordination
    RECOVERY_STATE_FILE = Path.home() / ".jarvis" / "trinity" / "gcp_recovery_state.json"

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._recovery_states: Dict[GCPFailureType, RecoveryState] = {}
        self._cascade_position: int = 0  # Current position in fallback cascade
        self._last_successful_tier: Optional[RoutingTier] = None
        self._recovery_lock: Optional[asyncio.Lock] = None

        # Cooldown configuration per failure type (seconds)
        self._cooldown_config: Dict[GCPFailureType, Tuple[float, float]] = {
            # (base_cooldown, max_cooldown)
            GCPFailureType.QUOTA: (300.0, 3600.0),       # 5 min base, 1 hour max
            GCPFailureType.CREDENTIALS: (600.0, 86400.0), # 10 min base, 24 hour max (needs manual fix)
            GCPFailureType.NETWORK: (10.0, 300.0),       # 10 sec base, 5 min max
            GCPFailureType.SPOT_UNAVAILABLE: (60.0, 600.0),  # 1 min base, 10 min max
            GCPFailureType.TIMEOUT: (30.0, 300.0),       # 30 sec base, 5 min max
            GCPFailureType.CONFIG: (600.0, 86400.0),     # 10 min base, 24 hour max (needs manual fix)
            GCPFailureType.OOM_RECOVERY: (60.0, 600.0),  # 1 min base, 10 min max
            GCPFailureType.UNKNOWN: (60.0, 600.0),       # 1 min base, 10 min max
        }

        # Load persisted state
        self._load_recovery_state()

    def _load_recovery_state(self) -> None:
        """Load recovery state from persistent storage."""
        try:
            if self.RECOVERY_STATE_FILE.exists():
                data = json.loads(self.RECOVERY_STATE_FILE.read_text())
                for type_name, state_data in data.get("states", {}).items():
                    try:
                        failure_type = GCPFailureType(type_name)
                        self._recovery_states[failure_type] = RecoveryState(
                            failure_type=failure_type,
                            failure_count=state_data.get("failure_count", 0),
                            last_failure_time=state_data.get("last_failure_time", 0.0),
                            cooldown_until=state_data.get("cooldown_until", 0.0),
                            recovery_attempts=state_data.get("recovery_attempts", 0),
                            permanently_disabled=state_data.get("permanently_disabled", False),
                            disable_reason=state_data.get("disable_reason"),
                        )
                    except ValueError:
                        pass  # Unknown failure type, skip
                self._cascade_position = data.get("cascade_position", 0)
                self.logger.debug(f"[v153.0] Loaded recovery state: {len(self._recovery_states)} failure types tracked")
        except Exception as e:
            self.logger.debug(f"[v153.0] Could not load recovery state: {e}")

    def _save_recovery_state(self) -> None:
        """Persist recovery state for cross-repo coordination."""
        try:
            self.RECOVERY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "states": {
                    state.failure_type.value: {
                        "failure_count": state.failure_count,
                        "last_failure_time": state.last_failure_time,
                        "cooldown_until": state.cooldown_until,
                        "recovery_attempts": state.recovery_attempts,
                        "permanently_disabled": state.permanently_disabled,
                        "disable_reason": state.disable_reason,
                    }
                    for state in self._recovery_states.values()
                },
                "cascade_position": self._cascade_position,
                "last_successful_tier": self._last_successful_tier.value if self._last_successful_tier else None,
                "updated_at": time.time(),
                "version": "v153.0",
            }
            self.RECOVERY_STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self.logger.debug(f"[v153.0] Could not save recovery state: {e}")

    def classify_failure(self, error: Exception, error_message: str = "") -> GCPFailureType:
        """
        v153.0: Classify a GCP failure for intelligent recovery.

        Uses error message patterns to determine the failure type.
        """
        msg = str(error).lower() + error_message.lower()

        # Quota failures
        if any(kw in msg for kw in ["quota", "limit exceeded", "resource exhausted", "429"]):
            return GCPFailureType.QUOTA

        # Credential failures
        if any(kw in msg for kw in ["credentials", "authentication", "unauthorized", "403", "permission denied"]):
            return GCPFailureType.CREDENTIALS

        # Network failures
        if any(kw in msg for kw in ["network", "connection", "dns", "timeout", "unreachable", "502", "503", "504"]):
            return GCPFailureType.NETWORK

        # Spot VM availability
        if any(kw in msg for kw in ["spot", "preemptible", "capacity", "zone", "unavailable"]):
            return GCPFailureType.SPOT_UNAVAILABLE

        # Timeout
        if any(kw in msg for kw in ["timeout", "timed out", "deadline"]):
            return GCPFailureType.TIMEOUT

        # Configuration issues
        if any(kw in msg for kw in ["config", "invalid", "malformed", "missing required"]):
            return GCPFailureType.CONFIG

        # OOM recovery
        if any(kw in msg for kw in ["oom", "memory", "out of memory"]):
            return GCPFailureType.OOM_RECOVERY

        return GCPFailureType.UNKNOWN

    def record_failure(self, failure_type: GCPFailureType, error: Optional[Exception] = None) -> RecoveryState:
        """
        v153.0: Record a failure and calculate cooldown.

        Returns the updated recovery state with calculated cooldown.
        """
        now = time.time()

        # Get or create recovery state
        if failure_type not in self._recovery_states:
            self._recovery_states[failure_type] = RecoveryState(failure_type=failure_type)

        state = self._recovery_states[failure_type]
        state.failure_count += 1
        state.last_failure_time = now

        # Calculate cooldown based on failure type and count
        base, max_cooldown = self._cooldown_config.get(failure_type, (60.0, 600.0))
        cooldown = state.calculate_backoff(base, max_cooldown)
        state.cooldown_until = now + cooldown

        # Check for permanent disable conditions
        if failure_type in (GCPFailureType.CREDENTIALS, GCPFailureType.CONFIG):
            if state.failure_count >= 3:
                state.permanently_disabled = True
                state.disable_reason = f"{failure_type.value} failure occurred {state.failure_count} times"
                self.logger.warning(
                    f"[v153.0] GCP permanently disabled due to {failure_type.value}: "
                    f"{state.disable_reason}"
                )

        # Log the failure and cooldown
        self.logger.info(
            f"[v153.0] GCP failure recorded: type={failure_type.value}, "
            f"count={state.failure_count}, cooldown={cooldown:.1f}s"
        )

        # Persist state
        self._save_recovery_state()

        return state

    def record_success(self, tier: RoutingTier) -> None:
        """
        v153.0: Record a successful operation to reset failure counts.
        """
        self._last_successful_tier = tier

        # Reset cascade position on success
        self._cascade_position = 0

        # Reset failure counts for transient failure types
        transient_types = {
            GCPFailureType.NETWORK,
            GCPFailureType.TIMEOUT,
            GCPFailureType.SPOT_UNAVAILABLE,
            GCPFailureType.OOM_RECOVERY,
            GCPFailureType.UNKNOWN,
        }

        for failure_type in transient_types:
            if failure_type in self._recovery_states:
                state = self._recovery_states[failure_type]
                if not state.permanently_disabled:
                    state.failure_count = max(0, state.failure_count - 1)
                    state.cooldown_until = 0.0

        self._save_recovery_state()
        self.logger.debug(f"[v153.0] Success recorded for tier {tier.value}")

    def can_attempt_gcp(self) -> Tuple[bool, Optional[str]]:
        """
        v153.0: Check if GCP provisioning can be attempted.

        Returns:
            Tuple of (can_attempt, reason_if_not)
        """
        # Check all relevant failure types
        for failure_type, state in self._recovery_states.items():
            if state.permanently_disabled:
                return False, f"Permanently disabled: {state.disable_reason}"

            if state.is_in_cooldown():
                remaining = state.cooldown_until - time.time()
                return False, f"In cooldown for {failure_type.value}: {remaining:.1f}s remaining"

        return True, None

    def get_fallback_tier(self, prefer_local: bool = False) -> Tuple[RoutingTier, str]:
        """
        v153.0: Get the next appropriate fallback tier.

        Returns:
            Tuple of (tier, reason)
        """
        # Fallback cascade order
        cascade = [
            RoutingTier.GCP_CLOUD_RUN,  # Try serverless first (no provisioning needed)
            RoutingTier.CLOUD_CLAUDE,   # API fallback
            RoutingTier.DEGRADED_LOCAL, # Minimal functionality
        ]

        if prefer_local:
            cascade.insert(0, RoutingTier.DEGRADED_LOCAL)

        # Find next available tier in cascade
        while self._cascade_position < len(cascade):
            tier = cascade[self._cascade_position]
            self._cascade_position += 1

            # For now, just return the tier - actual availability is checked elsewhere
            self._save_recovery_state()
            return tier, f"Fallback cascade position {self._cascade_position}"

        # All tiers exhausted, reset and use degraded local
        self._cascade_position = 0
        return RoutingTier.DEGRADED_LOCAL, "All fallback tiers exhausted"

    def reset_cascade(self) -> None:
        """v153.0: Reset the fallback cascade position."""
        self._cascade_position = 0
        self._save_recovery_state()

    def clear_all_cooldowns(self) -> None:
        """v153.0: Clear all cooldowns (for manual recovery)."""
        for state in self._recovery_states.values():
            state.cooldown_until = 0.0
            if not state.permanently_disabled:
                state.failure_count = 0
        self._save_recovery_state()
        self.logger.info("[v153.0] All GCP cooldowns cleared")


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
                    failure_threshold=5,  # v93.0: Increased from 3 for better resilience
                    timeout_seconds=60.0,  # v2.1: Fixed - was 'recovery_timeout' which doesn't exist
                    half_open_max_calls=2,
                    # v93.0: Startup-aware configuration for ML model loading
                    startup_grace_period_seconds=180.0,  # 3 minutes for ML models
                    startup_failure_threshold=30,
                    startup_network_failure_threshold=20,
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
        self._active_requests: Dict[RoutingTier, int] = {tier: 0 for tier in RoutingTier}

        # v266.0: VM Lifecycle State Machine (replaces scattered booleans)
        self._vm_lifecycle_state: VMLifecycleState = VMLifecycleState.IDLE
        self._vm_lifecycle_changed_at: float = 0.0
        self._trigger_readings: Deque[bool] = deque(maxlen=GCP_TRIGGER_READINGS_WINDOW)
        self._active_stability_count: int = 0
        self._cooling_started_at: float = 0.0
        self._model_unload_task: Optional[asyncio.Task] = None
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

        # v92.0: Memory pressure cooldown and backoff
        self._last_pressure_event: float = 0.0
        self._pressure_cooldown_base: float = float(os.getenv("MEMORY_PRESSURE_COOLDOWN", "60.0"))  # 60s base cooldown
        self._pressure_consecutive_failures: int = 0
        self._pressure_max_backoff: float = float(os.getenv("MEMORY_PRESSURE_MAX_BACKOFF", "600.0"))  # 10 min max
        self._gcp_permanently_unavailable: bool = False  # True if GCP is disabled/unconfigured

        # v93.0: Predictive Memory Defense - Adaptive monitoring and rate-of-change detection
        # Memory history for derivative (rate-of-change) calculation
        self._memory_history: Deque[Tuple[float, float]] = deque(maxlen=10)  # (timestamp, used_mb)
        self._last_memory_rate_check: float = 0.0
        self._current_memory_rate_mb_sec: float = 0.0  # Current rate of memory growth (MB/sec)

        # v266.1: Emergency offload state — 3-step escalation ladder
        self._emergency_offload_active: bool = False
        self._emergency_offload_started_at: float = 0.0
        self._pre_unload_percent: float = 0.0  # RAM % at Step 1 trigger (verification baseline)
        self._clean_unload_fired: bool = False  # Step 1 completed
        self._clean_unload_verified: bool = False  # Post-unload verification passed

        # SIGSTOP state (last resort only)
        self._sigstop_active: bool = False
        self._sigstop_started_at: float = 0.0  # When SIGSTOP was actually sent
        self._paused_processes: Dict[int, str] = {}  # pid -> process_name
        self._sigstop_released_at: float = 0.0  # Cooldown tracking for SIGSTOP only

        # Hysteresis (applies to all emergency actions)
        self._emergency_offload_hysteresis_armed: bool = False

        # v266.2: Post-crisis recovery state
        self._model_needs_recovery: bool = False
        self._recovery_stable_since: float = 0.0  # When RAM first dropped below recovery threshold
        self._recovery_attempts: int = 0
        self._recovery_in_progress: bool = False  # Prevents concurrent reload attempts
        self._recovery_cooldown_until: float = 0.0
        self._recovery_task: Optional[asyncio.Task] = None
        self._recovery_percent_history: Deque[Tuple[float, float]] = deque(maxlen=8)
        self._local_circuit_state: str = "unknown"
        self._local_warmup_hook_registered: bool = False
        self._model_serving_ref: Optional[Any] = None
        self._memory_quantizer: Optional[Any] = None
        self._memory_tier_change_callback: Optional[Callable] = None
        self._startup_router_started: bool = False
        self._memory_callbacks_registered: bool = False
        self._memory_callback_registration_reason: str = "not_started"
        self._startup_contract_event: Optional[asyncio.Event] = None

        # Process tracking for emergency offload
        self._ml_loader_ref = None  # Reference to ProcessIsolatedMLLoader
        self._local_llm_pids: Set[int] = set()  # PIDs of local LLM processes to pause

        # Adaptive polling state
        self._current_poll_interval: float = MEMORY_NORMAL_POLL_INTERVAL
        self._in_fast_polling_mode: bool = False

        # Initialize VM provisioning lock if available
        if self._vm_provisioning_enabled and _VM_LOCK_AVAILABLE and DistributedLock:
            try:
                _lock_cfg = DistributedLockConfig(
                    lock_ttl=VM_PROVISIONING_LOCK_TTL,
                    default_timeout=30.0,
                    retry_interval=1.0,
                )
                _ctor_params = set(inspect.signature(DistributedLock.__init__).parameters.keys())
                _lock_kwargs: Dict[str, Any] = {"config": _lock_cfg}
                if "name" in _ctor_params:
                    _lock_kwargs["name"] = "gcp_vm_provisioning"
                elif "lock_name" in _ctor_params:
                    _lock_kwargs["lock_name"] = "gcp_vm_provisioning"
                else:
                    raise TypeError(
                        "DistributedLock constructor missing both `name` and `lock_name`"
                    )
                self._vm_provisioning_lock = DistributedLock(**_lock_kwargs)
            except Exception as e:
                self.logger.warning(f"VM provisioning lock initialization failed: {e}")

        # v152.0: Cloud lock cache for authoritative cloud state
        self._cloud_lock_cache: Optional[Dict[str, Any]] = None
        self._cloud_lock_cache_time: float = 0.0
        self._cloud_lock_cache_ttl: float = 5.0  # Refresh every 5 seconds

        # v153.0: Recovery Cascade Manager for intelligent GCP failure handling
        self._recovery_cascade = RecoveryCascadeManager(self.logger)

    # =========================================================================
    # v152.0: AUTHORITATIVE CLOUD STATE METHODS
    # =========================================================================
    # These methods make GCPHybridPrimeRouter the single source of truth for
    # cloud mode state. All components MUST check these methods before attempting
    # local operations.
    # =========================================================================

    def is_cloud_locked(self) -> bool:
        """
        v152.0: AUTHORITATIVE check for cloud-only mode.

        This is the SINGLE SOURCE OF TRUTH for whether local model operations
        should be skipped. All components MUST call this before attempting
        local model discovery, loading, or inference.

        Checks (in order):
        1. JARVIS_GCP_OFFLOAD_ACTIVE environment variable (set by supervisor)
        2. cloud_lock.json persistent state (survives restarts)
        3. Emergency offload active state (memory pressure)

        Returns:
            True if cloud mode is active and local operations should be SKIPPED
        """
        # Check 1: Environment variable (fastest, set by supervisor)
        gcp_offload_active = os.getenv("JARVIS_GCP_OFFLOAD_ACTIVE", "false").lower() == "true"
        if gcp_offload_active:
            return True

        # Check 2: Emergency offload state (memory pressure)
        if self._emergency_offload_active:
            return True

        # Check 3: Persistent cloud lock file (with caching)
        try:
            now = time.time()
            if (self._cloud_lock_cache is None or
                    (now - self._cloud_lock_cache_time) > self._cloud_lock_cache_ttl):
                # Refresh cache
                from pathlib import Path
                cloud_lock_file = Path.home() / ".jarvis" / "trinity" / "cloud_lock.json"
                if cloud_lock_file.exists():
                    self._cloud_lock_cache = json.loads(cloud_lock_file.read_text())
                else:
                    self._cloud_lock_cache = {"locked": False}
                self._cloud_lock_cache_time = now

            if self._cloud_lock_cache and self._cloud_lock_cache.get("locked", False):
                return True

        except Exception as e:
            self.logger.debug(f"[v152.0] Cloud lock check error: {e}")

        return False

    def get_cloud_lock_reason(self) -> Optional[str]:
        """
        v152.0: Get the reason for cloud lock (for logging/diagnostics).

        Returns:
            Reason string if cloud locked, None otherwise
        """
        if os.getenv("JARVIS_GCP_OFFLOAD_ACTIVE", "false").lower() == "true":
            return "JARVIS_GCP_OFFLOAD_ACTIVE=true"

        if self._emergency_offload_active:
            return f"Emergency offload (RAM critical)"

        if self._cloud_lock_cache and self._cloud_lock_cache.get("locked", False):
            return self._cloud_lock_cache.get("reason", "cloud_lock.json")

        return None

    def get_active_discovery_endpoint(self) -> Optional[str]:
        """
        v152.0: Get the active model discovery endpoint.

        This is the AUTHORITATIVE method for determining which endpoint to use
        for model discovery. Components MUST use this instead of hardcoding
        localhost:8000.

        Returns:
            - GCP Cloud Run URL if cloud mode is active
            - GCP VM endpoint if VM is available
            - None if local endpoint should be used (caller uses localhost)

        Usage:
            endpoint = router.get_active_discovery_endpoint()
            if endpoint:
                # Use GCP endpoint - skip local discovery
                api_base = endpoint
            else:
                # Use local endpoint
                api_base = "http://localhost:8000/v1"
        """
        # If cloud locked, ALWAYS return cloud endpoint
        if self.is_cloud_locked():
            # Priority 1: Cloud Run URL (always available, serverless)
            cloud_run_url = os.getenv("JARVIS_PRIME_CLOUD_RUN_URL")
            if cloud_run_url:
                self.logger.debug(f"[v152.0] Cloud locked - using Cloud Run: {cloud_run_url}")
                return cloud_run_url

            # Priority 2: GCP VM endpoint (if provisioned)
            if self._gcp_controller and hasattr(self._gcp_controller, 'get_vm_endpoint'):
                try:
                    vm_endpoint = self._gcp_controller.get_vm_endpoint()
                    if vm_endpoint:
                        self.logger.debug(f"[v152.0] Cloud locked - using GCP VM: {vm_endpoint}")
                        return vm_endpoint
                except Exception:
                    pass

            # Priority 3: Fallback to Cloud Run URL from config
            gcp_region = os.getenv("GCP_REGION", "us-central1")
            gcp_project = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", ""))
            if gcp_project:
                fallback_url = f"https://jarvis-prime-{gcp_region}-{gcp_project}.a.run.app/v1"
                self.logger.debug(f"[v152.0] Cloud locked - using fallback: {fallback_url}")
                return fallback_url

            # No cloud endpoint available - log warning but still return None
            # This allows graceful degradation to offline mode
            self.logger.warning(
                "[v152.0] Cloud locked but no GCP endpoint configured. "
                "Set JARVIS_PRIME_CLOUD_RUN_URL or GCP_PROJECT_ID."
            )
            return None

        # Not cloud locked - check if GCP VM is available (opportunistic)
        if self._gcp_controller and hasattr(self._gcp_controller, 'is_vm_available'):
            if self._gcp_controller.is_vm_available():
                try:
                    vm_endpoint = self._gcp_controller.get_vm_endpoint()
                    if vm_endpoint:
                        self.logger.debug(f"[v152.0] GCP VM available - using: {vm_endpoint}")
                        return vm_endpoint
                except Exception:
                    pass

        # Use local endpoint (return None, caller uses localhost)
        return None

    def should_skip_local_discovery(self) -> bool:
        """
        v152.0: Determine if local model discovery should be skipped entirely.

        This is a convenience method that components can call to determine
        if they should skip local discovery attempts completely.

        When True:
        - Do NOT attempt to connect to localhost:8000
        - Do NOT record circuit breaker failures for local endpoints
        - Return cached/offline models immediately

        Returns:
            True if local discovery should be skipped
        """
        return self.is_cloud_locked()

    def _get_startup_contract_event(self) -> asyncio.Event:
        if self._startup_contract_event is None:
            self._startup_contract_event = asyncio.Event()
            if self._startup_router_started and self._memory_callbacks_registered:
                self._startup_contract_event.set()
        return self._startup_contract_event

    def _set_startup_contract_state(
        self,
        *,
        router_started: Optional[bool] = None,
        memory_callbacks_registered: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> None:
        if router_started is not None:
            self._startup_router_started = bool(router_started)
        if memory_callbacks_registered is not None:
            self._memory_callbacks_registered = bool(memory_callbacks_registered)
        if reason is not None:
            self._memory_callback_registration_reason = str(reason)
        ready = self._startup_router_started and self._memory_callbacks_registered
        if self._startup_contract_event is not None:
            if ready:
                self._startup_contract_event.set()
            else:
                self._startup_contract_event.clear()

    def get_startup_contract_status(self) -> Dict[str, Any]:
        """Return deterministic router startup contract status."""
        return {
            "router_started": self._startup_router_started,
            "memory_callbacks_registered": self._memory_callbacks_registered,
            "ready": self._startup_router_started and self._memory_callbacks_registered,
            "running": self._running,
            "memory_callback_registration_reason": self._memory_callback_registration_reason,
            "vm_provisioning_enabled": self._vm_provisioning_enabled,
            "gcp_permanently_unavailable": self._gcp_permanently_unavailable,
            "timestamp": time.time(),
        }

    async def wait_for_startup_contract(
        self,
        timeout_seconds: float = 20.0,
    ) -> Dict[str, Any]:
        """Wait until router startup contract is satisfied or timeout expires."""
        status = self.get_startup_contract_status()
        if status["ready"] or timeout_seconds <= 0.0:
            return status
        event = self._get_startup_contract_event()
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.1, timeout_seconds))
        except asyncio.TimeoutError:
            pass
        return self.get_startup_contract_status()

    def _build_memory_tier_change_callback(self) -> Callable:
        async def _on_tier_change(old_tier, new_tier):
            """MemoryQuantizer authoritative tier change callback."""
            if not self._running or not self._vm_provisioning_enabled or self._gcp_permanently_unavailable:
                return
            tier_severity = {
                "abundant": 0, "optimal": 1,
                "elevated": 2, "constrained": 3,
                "critical": 4, "emergency": 5,
            }
            old_name = old_tier.value if hasattr(old_tier, 'value') else str(old_tier)
            new_name = new_tier.value if hasattr(new_tier, 'value') else str(new_tier)
            new_sev = tier_severity.get(new_name.lower(), 0)

            if new_sev >= 4 and self._vm_lifecycle_state == VMLifecycleState.IDLE:
                self.logger.info(
                    f"[VMLifecycle] MemoryQuantizer tier change: {old_name} -> {new_name}"
                )
                self._trigger_readings.append(True)
                above_count = sum(1 for r in self._trigger_readings if r)
                if above_count >= GCP_TRIGGER_READINGS_REQUIRED:
                    self._transition_vm_lifecycle(
                        VMLifecycleState.TRIGGERING,
                        f"mq_tier_{new_name}"
                    )

        return _on_tier_change

    async def _register_memory_callbacks(self) -> bool:
        """Register memory callbacks required by router startup contract."""
        if self._memory_callbacks_registered:
            return True
        try:
            from backend.core.memory_quantizer import get_memory_quantizer
            mq = await get_memory_quantizer()
            self._memory_quantizer = mq
            if not mq:
                self._set_startup_contract_state(
                    memory_callbacks_registered=False,
                    reason="memory_quantizer_unavailable",
                )
                return False
            if not hasattr(mq, 'register_tier_change_callback'):
                self._set_startup_contract_state(
                    memory_callbacks_registered=False,
                    reason="tier_callback_api_missing",
                )
                return False
            callback = self._build_memory_tier_change_callback()
            mq.register_tier_change_callback(callback)
            self._memory_tier_change_callback = callback
            self._set_startup_contract_state(
                memory_callbacks_registered=True,
                reason="tier_callbacks_registered",
            )
            self.logger.info("[VMLifecycle] Registered MemoryQuantizer tier callback")
            return True
        except ImportError:
            self._set_startup_contract_state(
                memory_callbacks_registered=False,
                reason="memory_quantizer_import_unavailable",
            )
            self.logger.debug("MemoryQuantizer not available — callback contract not satisfied")
            return False
        except Exception as e:
            self._set_startup_contract_state(
                memory_callbacks_registered=False,
                reason=f"tier_callback_registration_failed:{e}",
            )
            self.logger.debug(f"MemoryQuantizer callback registration failed: {e}")
            return False

    async def start(self) -> bool:
        """Start the hybrid router."""
        if self._running:
            return True

        self._running = True
        self._set_startup_contract_state(
            router_started=False,
            memory_callbacks_registered=False,
            reason="starting",
        )
        self.logger.info("GCPHybridPrimeRouter v2.0 starting...")

        # Connect to integrations
        await self._connect_integrations()

        # Start monitoring
        self._monitoring_task = asyncio.create_task(
            self._monitoring_loop(),
            name="gcp_hybrid_prime_monitor",
        )
        self._set_startup_contract_state(router_started=True)

        callback_registered = await self._register_memory_callbacks()
        if not callback_registered:
            self.logger.warning(
                "[StartupContract] Router started but memory callbacks not registered (%s)",
                self._memory_callback_registration_reason,
            )

        self.logger.info(
            f"GCPHybridPrimeRouter ready "
            f"(local_min_ram: {LOCAL_PRIME_MIN_RAM_GB}GB, "
            f"gcp_trigger: {GCP_TRIGGER_RAM_PERCENT}%, "
            f"resilience: {self._use_resilience})"
        )

        # v92.0: Pre-flight check for GCP configuration BEFORE starting monitor
        # This prevents the first memory pressure event from triggering if GCP is disabled
        if self._vm_provisioning_enabled:
            # Check if GCP is actually configured and enabled
            gcp_enabled = os.getenv("GCP_ENABLED", "false").lower() == "true"
            gcp_project = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", ""))

            if not gcp_enabled:
                self.logger.info(
                    "GCPHybridPrimeRouter: GCP_ENABLED=false, memory pressure monitoring disabled. "
                    "Set GCP_ENABLED=true to enable VM provisioning."
                )
                self._gcp_permanently_unavailable = True
            elif not gcp_project:
                self.logger.info(
                    "GCPHybridPrimeRouter: GCP_PROJECT_ID not set, memory pressure monitoring disabled. "
                    "Set GCP_PROJECT_ID to enable VM provisioning."
                )
                self._gcp_permanently_unavailable = True
            else:
                # GCP is properly configured - start monitoring

                self._memory_pressure_task = asyncio.create_task(
                    self._memory_pressure_monitor(),
                    name="gcp_memory_pressure_monitor",
                )
                self.logger.info(
                    f"GCPHybridPrimeRouter: Memory pressure monitoring active "
                    f"(project: {gcp_project[:10]}..., threshold: {VM_PROVISIONING_THRESHOLD}%)"
                )

        return True

    async def _memory_pressure_monitor(self) -> None:
        """
        v93.0: Predictive Memory Defense - Enhanced monitoring with:
        - Adaptive polling (1s when RAM > 60%, 5s otherwise)
        - Rate-of-change (derivative) trigger for memory spikes
        - Emergency offload with SIGSTOP/SIGCONT
        - Intelligent cooldown with exponential backoff

        When RAM usage exceeds VM_PROVISIONING_THRESHOLD OR memory growth rate
        exceeds MEMORY_SPIKE_RATE_THRESHOLD_MB (100MB/sec), triggers GCP provisioning.

        At EMERGENCY_UNLOAD_RAM_PERCENT (85%), initiates emergency offload:
        1. SIGSTOP all local LLM processes
        2. Provision GCP VM
        3. SIGCONT processes (or terminate if cloud ready)
        """
        while self._running:
            try:
                # v93.0: Adaptive polling - faster checks when memory pressure detected
                await asyncio.sleep(self._current_poll_interval)

                # v92.0: Early exit if GCP is permanently unavailable
                if self._gcp_permanently_unavailable:
                    # Only log periodically (every 10 minutes) to avoid spam
                    if time.time() - self._last_pressure_event > 600:
                        self.logger.debug(
                            "Memory pressure monitor: GCP unavailable, skipping checks"
                        )
                        self._last_pressure_event = time.time()
                    continue

                # Get RAM info with MB calculation for rate tracking
                ram_info = await self._get_ram_info_with_mb()
                if not ram_info:
                    continue

                used_percent = ram_info.get("used_percent", 0)
                used_mb = ram_info.get("used_mb", 0)
                timestamp = time.time()

                # v93.0: Update memory history for rate-of-change calculation
                self._memory_history.append((timestamp, used_mb))
                self._recovery_percent_history.append((timestamp, float(used_percent)))

                # v93.0: Calculate memory growth rate (derivative)
                memory_rate_mb_sec = self._calculate_memory_rate()
                self._current_memory_rate_mb_sec = memory_rate_mb_sec

                # v93.0: Adaptive polling adjustment
                if used_percent >= MEMORY_ADAPTIVE_POLL_THRESHOLD:
                    if not self._in_fast_polling_mode:
                        self._in_fast_polling_mode = True
                        self._current_poll_interval = MEMORY_FAST_POLL_INTERVAL
                        self.logger.info(
                            f"[v93.0] Entering fast polling mode (RAM: {used_percent:.1f}%, "
                            f"rate: {memory_rate_mb_sec:.1f} MB/s)"
                        )
                        # Signal elevated status to other repos
                        await self._signal_memory_pressure_to_repos(
                            status="elevated",
                            action="reduce_load",
                            used_percent=used_percent,
                            rate_mb_sec=memory_rate_mb_sec,
                        )
                else:
                    if self._in_fast_polling_mode:
                        self._in_fast_polling_mode = False
                        self._current_poll_interval = MEMORY_NORMAL_POLL_INTERVAL
                        self.logger.info(
                            f"[v93.0] Exiting fast polling mode (RAM: {used_percent:.1f}%)"
                        )
                        # Signal normal status to other repos
                        await self._signal_memory_pressure_to_repos(
                            status="normal",
                            action=None,
                            used_percent=used_percent,
                            rate_mb_sec=memory_rate_mb_sec,
                        )

                # v266.1: Emergency offload — 3-step escalation ladder
                # Step 1 (85%): Clean model unload via COMPONENT_UNLOAD
                # Step 2: Post-unload verification + GCP attempt
                # Step 3 (95%): SIGSTOP as one-shot last resort
                if not self._emergency_offload_active:
                    # Check hysteresis: must wait for RAM to drop before re-trigger
                    if self._emergency_offload_hysteresis_armed:
                        hysteresis_threshold = EMERGENCY_UNLOAD_RAM_PERCENT - EMERGENCY_OFFLOAD_HYSTERESIS
                        if used_percent < hysteresis_threshold:
                            self._emergency_offload_hysteresis_armed = False
                            self._clean_unload_fired = False
                            self._clean_unload_verified = False
                            # v266.2: Trigger post-crisis recovery if model was unloaded
                            if not self._model_needs_recovery:
                                self._model_needs_recovery = True
                                self._recovery_stable_since = 0.0
                                self._recovery_attempts = 0
                                self._recovery_cooldown_until = 0.0
                            self.logger.info(
                                f"[v266.2] RAM dropped to {used_percent:.1f}% — "
                                f"hysteresis disarmed, escalation reset, "
                                f"recovery armed"
                            )
                        # else: Still above hysteresis threshold, skip to VM state machine

                    # Step 1: Clean model unload at CRITICAL tier (85%+)
                    elif used_percent >= EMERGENCY_UNLOAD_RAM_PERCENT and not self._clean_unload_fired:
                        self.logger.warning(
                            f"[v266.1] RAM at {used_percent:.1f}% (>={EMERGENCY_UNLOAD_RAM_PERCENT}%) "
                            f"— firing COMPONENT_UNLOAD for clean model unload"
                        )
                        self._emergency_offload_active = True
                        self._emergency_offload_started_at = time.time()
                        self._pre_unload_percent = used_percent  # Baseline for verification
                        self._clean_unload_fired = True
                        self._clean_unload_verified = False
                        self._local_circuit_state = "open"

                        # Fire COMPONENT_UNLOAD via MemoryQuantizer
                        try:
                            from backend.core.memory_quantizer import (
                                get_memory_quantizer, OptimizationStrategy,
                            )
                            _mq = await get_memory_quantizer()
                            if _mq:
                                await _mq._apply_strategy(OptimizationStrategy.COMPONENT_UNLOAD)
                                self.logger.info("[v266.1] COMPONENT_UNLOAD fired — waiting for verification")
                            else:
                                self.logger.warning("[v266.1] MemoryQuantizer unavailable")
                        except Exception as e:
                            self.logger.error(f"[v266.1] COMPONENT_UNLOAD failed: {e}")

                        # Signal to other repos
                        await self._signal_memory_pressure_to_repos(
                            status="offload_active",
                            action="unload",
                            used_percent=used_percent,
                            rate_mb_sec=memory_rate_mb_sec,
                        )
                        continue

                elif self._emergency_offload_active:
                    # Active offload — check escalation ladder
                    await self._check_offload_escalation(used_percent, memory_rate_mb_sec)
                    continue

                # v266.2: Post-crisis model recovery check
                if self._model_needs_recovery and not self._emergency_offload_active:
                    await self._check_model_recovery(used_percent)

                # v266.0: State-machine-driven VM provisioning
                spike_detected = memory_rate_mb_sec >= MEMORY_SPIKE_RATE_THRESHOLD_MB
                current_state = self._vm_lifecycle_state

                if current_state == VMLifecycleState.IDLE:
                    above_trigger = used_percent >= CRITICAL_RAM_PERCENT  # 85%
                    self._trigger_readings.append(above_trigger)

                    # Spike bypass: 100MB/sec rate triggers instantly
                    if spike_detected and not self._is_in_cooldown():
                        self.logger.warning(
                            f"[v93.0] Memory SPIKE detected: {memory_rate_mb_sec:.1f} MB/s "
                            f"(threshold: {MEMORY_SPIKE_RATE_THRESHOLD_MB} MB/s), "
                            f"current RAM: {used_percent:.1f}%"
                        )
                        # Clear stale readings to avoid contaminating future N-of-M checks
                        self._trigger_readings.clear()
                        self._transition_vm_lifecycle(VMLifecycleState.TRIGGERING,
                                                       f"memory_spike_{memory_rate_mb_sec:.0f}mb_sec")
                        self._transition_vm_lifecycle(VMLifecycleState.PROVISIONING,
                                                       "spike_bypass")
                        self._last_pressure_event = time.time()
                        success = await self._trigger_vm_provisioning(
                            reason=f"memory_spike_{memory_rate_mb_sec:.0f}mb_sec"
                        )
                        if not success:
                            self._transition_vm_lifecycle(VMLifecycleState.COOLING_DOWN,
                                                           "provisioning_failed")
                            self._cooling_started_at = time.time()
                        self._handle_provisioning_result(success)
                    elif above_trigger:
                        self._transition_vm_lifecycle(VMLifecycleState.TRIGGERING,
                                                       f"pressure_{used_percent:.1f}pct")

                elif current_state == VMLifecycleState.TRIGGERING:
                    above_trigger = used_percent >= CRITICAL_RAM_PERCENT
                    self._trigger_readings.append(above_trigger)

                    above_count = sum(1 for r in self._trigger_readings if r)

                    if above_count >= GCP_TRIGGER_READINGS_REQUIRED:
                        # Sustained pressure confirmed — provision
                        self._transition_vm_lifecycle(VMLifecycleState.PROVISIONING,
                                                       f"sustained_{above_count}/{len(self._trigger_readings)}")
                        self._last_pressure_event = time.time()
                        success = await self._trigger_vm_provisioning(reason="sustained_pressure")
                        if not success:
                            self._transition_vm_lifecycle(VMLifecycleState.COOLING_DOWN,
                                                           "provisioning_failed")
                            self._cooling_started_at = time.time()
                        self._handle_provisioning_result(success)
                    elif not above_trigger and above_count == 0:
                        # Pressure gone completely — back to idle
                        self._transition_vm_lifecycle(VMLifecycleState.IDLE, "pressure_cleared")
                        self._trigger_readings.clear()

                elif current_state == VMLifecycleState.ACTIVE:
                    # Check release threshold (hysteresis: must drop below 70%)
                    if used_percent < GCP_RELEASE_RAM_PERCENT:
                        self._transition_vm_lifecycle(VMLifecycleState.COOLING_DOWN,
                                                       f"pressure_released_{used_percent:.1f}pct")
                        self._cooling_started_at = time.time()

                elif current_state == VMLifecycleState.COOLING_DOWN:
                    elapsed = time.time() - self._cooling_started_at
                    if elapsed >= GCP_COOLING_GRACE_SECONDS:
                        self._transition_vm_lifecycle(VMLifecycleState.IDLE, "cooling_complete")
                        self._trigger_readings.clear()
                    elif used_percent >= CRITICAL_RAM_PERCENT:
                        # Pressure back — return to triggering (VM still running)
                        self._transition_vm_lifecycle(VMLifecycleState.TRIGGERING,
                                                       "pressure_returned")

                # PROVISIONING and BOOTING states are managed by _trigger_vm_provisioning()
                # and health check callbacks — not by the pressure monitor

                # Check if we should terminate VM (low usage, only in IDLE)
                if current_state == VMLifecycleState.IDLE and used_percent < GCP_TRIGGER_RAM_PERCENT - 20:
                    await self._check_vm_termination()

                # Keep recovery/circuit observability fresh while post-crisis recovery is active.
                if self._model_needs_recovery or self._recovery_state() in {"reloading", "cooldown"}:
                    signal_status = "critical" if used_percent >= CRITICAL_RAM_PERCENT else "elevated"
                    await self._signal_memory_pressure_to_repos(
                        status=signal_status,
                        action="recover" if self._model_needs_recovery else None,
                        used_percent=used_percent,
                        rate_mb_sec=memory_rate_mb_sec,
                    )

            except asyncio.CancelledError:
                # Ensure processes are resumed on shutdown
                if self._emergency_offload_active:
                    await self._release_emergency_offload(reason="shutdown")
                break
            except Exception as e:
                self.logger.error(f"Memory pressure monitor error: {e}")
                await asyncio.sleep(10.0)

    def _is_in_cooldown(self) -> bool:
        """v93.0: Check if we're in cooldown period."""
        current_cooldown = min(
            self._pressure_cooldown_base * (2 ** self._pressure_consecutive_failures),
            self._pressure_max_backoff
        )
        time_since_last = time.time() - self._last_pressure_event
        return time_since_last < current_cooldown

    def _handle_provisioning_result(self, success: bool) -> None:
        """v93.0: Handle VM provisioning result."""
        if success:
            self._pressure_consecutive_failures = 0
        else:
            self._pressure_consecutive_failures += 1
            if self._pressure_consecutive_failures >= 3:
                current_cooldown = min(
                    self._pressure_cooldown_base * (2 ** self._pressure_consecutive_failures),
                    self._pressure_max_backoff
                )
                self.logger.info(
                    f"VM provisioning failed {self._pressure_consecutive_failures} times, "
                    f"backing off for {current_cooldown:.0f}s"
                )

    async def _get_ram_info_with_mb(self) -> Optional[dict]:
        """v93.0: Get RAM info including used MB for rate calculation."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            now = time.time()

            info = {
                "total_gb": mem.total / (1024**3),
                "available_gb": mem.available / (1024**3),
                "used_percent": mem.percent,
                "used_mb": (mem.total - mem.available) / (1024**2),  # Used in MB
                "total_mb": mem.total / (1024**2),
                "timestamp": now,
            }

            # Update cache
            self._last_ram_check = now
            self._cached_ram_info = info
            return info

        except Exception as e:
            self.logger.warning(f"RAM check failed: {e}")
            return None

    def _calculate_memory_rate(self) -> float:
        """
        v93.0: Calculate memory growth rate (MB/sec) using derivative.

        Uses linear regression over the memory history window for stability.
        Returns positive values for growth, negative for decline.
        """
        if len(self._memory_history) < 2:
            return 0.0

        # Get readings within the derivative window
        now = time.time()
        window_start = now - MEMORY_DERIVATIVE_WINDOW_SEC

        recent_readings = [
            (ts, mb) for ts, mb in self._memory_history
            if ts >= window_start
        ]

        if len(recent_readings) < 2:
            return 0.0

        # Simple linear regression for rate calculation
        # Using least squares: slope = (n*sum(xy) - sum(x)*sum(y)) / (n*sum(x^2) - sum(x)^2)
        n = len(recent_readings)
        sum_t = sum(ts for ts, _ in recent_readings)
        sum_mb = sum(mb for _, mb in recent_readings)
        sum_t_mb = sum(ts * mb for ts, mb in recent_readings)
        sum_t_sq = sum(ts * ts for ts, _ in recent_readings)

        denominator = n * sum_t_sq - sum_t * sum_t
        if abs(denominator) < 1e-10:
            return 0.0

        slope = (n * sum_t_mb - sum_t * sum_mb) / denominator
        return slope  # MB per second

    def register_llm_pid(self, pid: int) -> None:
        """Register a JARVIS-owned LLM process PID for emergency offload targeting."""
        self._local_llm_pids.add(pid)

    def unregister_llm_pid(self, pid: int) -> None:
        """Unregister a JARVIS-owned LLM process PID."""
        self._local_llm_pids.discard(pid)

    async def _pause_local_llm_processes(self) -> int:
        """v266.1: Pause local LLM processes via SIGSTOP (last resort only).

        Priority order:
        1. JARVIS-owned tracked PIDs (_local_llm_pids) — always preferred
        2. ProcessIsolatedMLLoader PIDs — if available
        3. Pattern-based scan — fallback only when no tracked PIDs found

        Returns:
            Number of processes paused
        """
        paused_count = 0

        try:
            import psutil

            # Priority 1: Use tracked JARVIS-owned PIDs
            if self._local_llm_pids:
                self.logger.info(
                    f"[v266.1] SIGSTOP targeting {len(self._local_llm_pids)} tracked JARVIS PID(s)"
                )
                for pid in list(self._local_llm_pids):
                    if pid not in self._paused_processes and self._pause_process(pid, "tracked_llm"):
                        paused_count += 1
                if paused_count > 0:
                    return paused_count

            # Priority 2: ProcessIsolatedMLLoader PIDs
            if self._ml_loader_ref is None:
                try:
                    from backend.core.process_isolated_ml_loader import get_ml_loader
                    self._ml_loader_ref = await get_ml_loader()
                except Exception:
                    pass

            if self._ml_loader_ref and hasattr(self._ml_loader_ref, '_active_processes'):
                for pid in list(self._ml_loader_ref._active_processes.keys()):
                    if pid not in self._paused_processes and self._pause_process(pid, "ml_loader"):
                        paused_count += 1
                if paused_count > 0:
                    return paused_count

            # Priority 3: Pattern-based scan (fallback only)
            self.logger.warning(
                "[v266.1] No tracked PIDs — falling back to pattern-based process scan"
            )
            llm_patterns = [
                "ollama", "llama", "llama.cpp", "llamacpp",
                "text-generation", "vllm", "transformers",
                "jarvis-prime", "jarvis_prime",
            ]

            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    proc_info = proc.info
                    pid = proc_info['pid']

                    if pid in self._paused_processes:
                        continue

                    name = (proc_info.get('name') or '').lower()
                    cmdline = ' '.join(proc_info.get('cmdline') or []).lower()

                    is_llm_process = any(
                        pattern in name or pattern in cmdline
                        for pattern in llm_patterns
                    )

                    if is_llm_process:
                        self.logger.info(
                            f"[v266.1] Pattern match: PID {pid} ({name}) — sending SIGSTOP"
                        )
                        if self._pause_process(pid, name):
                            paused_count += 1

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        except Exception as e:
            self.logger.error(f"[v266.1] Error pausing LLM processes: {e}")

        return paused_count

    def _pause_process(self, pid: int, name: str) -> bool:
        """v93.0: Send SIGSTOP to a process."""
        try:
            import psutil

            # Verify process exists
            if not psutil.pid_exists(pid):
                return False

            # Skip system processes
            if pid <= 1:
                return False

            # Send SIGSTOP
            os.kill(pid, signal.SIGSTOP)
            self._paused_processes[pid] = name
            self.logger.debug(f"[v93.0] SIGSTOP sent to PID {pid} ({name})")
            return True

        except PermissionError:
            self.logger.warning(f"[v93.0] Permission denied pausing PID {pid}")
            return False
        except ProcessLookupError:
            return False
        except Exception as e:
            self.logger.warning(f"[v93.0] Error pausing PID {pid}: {e}")
            return False

    async def _check_offload_escalation(self, current_used_percent: float, rate_mb_sec: float) -> None:
        """v266.1: Check escalation ladder during active emergency offload.

        Step 1 complete (clean unload fired). Now:
        - Verify unload worked (wait, re-read memory)
        - If RAM still high, attempt GCP
        - If GCP fails and RAM >= 95%, one-shot SIGSTOP (no cycling)
        """
        elapsed = time.time() - self._emergency_offload_started_at

        # --- Post-unload verification gate ---
        if self._clean_unload_fired and not self._clean_unload_verified:
            if elapsed < EMERGENCY_UNLOAD_VERIFY_DELAY_SEC:
                # Still waiting for verification window
                return

            # Verification window elapsed — read memory
            try:
                import psutil
                mem = psutil.virtual_memory()
                post_unload_percent = mem.percent
                ram_dropped_gb = (self._pre_unload_percent - post_unload_percent) * mem.total / (100 * 1024 ** 3)
            except Exception:
                post_unload_percent = self._pre_unload_percent
                ram_dropped_gb = 0.0

            if ram_dropped_gb < 0:
                ram_dropped_gb = 0.0

            if ram_dropped_gb >= EMERGENCY_UNLOAD_MIN_DROP_GB or post_unload_percent < EMERGENCY_UNLOAD_RAM_PERCENT:
                # Unload worked — cancel escalation
                self.logger.info(
                    f"[v266.1] Post-unload verification PASSED: "
                    f"RAM {post_unload_percent:.1f}% (dropped {ram_dropped_gb:.1f}GB). "
                    f"Escalation stopped."
                )
                self._clean_unload_verified = True
                self._emergency_offload_active = False
                self._emergency_offload_hysteresis_armed = True
                await self._signal_memory_pressure_to_repos(
                    status="normal", action=None, used_percent=post_unload_percent,
                )
                return

            # Unload didn't drop enough — proceed to GCP
            self._clean_unload_verified = True  # Mark verified (failed, but checked)
            self.logger.warning(
                f"[v266.1] Post-unload verification FAILED: "
                f"RAM still {post_unload_percent:.1f}% (dropped only {ram_dropped_gb:.1f}GB). "
                f"Attempting GCP offload."
            )

            # Step 2: GCP attempt
            if not self._gcp_permanently_unavailable:
                can_attempt, cooldown_reason = self._recovery_cascade.can_attempt_gcp()
                if can_attempt:
                    self.logger.info("[v266.1] Provisioning GCP VM for workload transfer...")
                    success = await self._trigger_vm_provisioning(
                        reason=f"post_unload_escalation_ram_{post_unload_percent:.0f}pct"
                    )
                    if success:
                        self.logger.info("[v266.1] GCP VM provisioned — escalation resolved")
                        self._recovery_cascade.record_success(RoutingTier.GCP_VM)
                        self._emergency_offload_active = False
                        self._emergency_offload_hysteresis_armed = True
                        return
                    else:
                        self.logger.warning("[v266.1] GCP provisioning failed")
                else:
                    self.logger.warning(f"[v266.1] GCP in cooldown: {cooldown_reason}")

            return  # Wait for next poll cycle to check SIGSTOP threshold

        # --- SIGSTOP last resort gate ---
        if current_used_percent >= EMERGENCY_SIGSTOP_RAM_PERCENT and not self._sigstop_active:
            # Check SIGSTOP cooldown
            time_since_sigstop = time.time() - self._sigstop_released_at
            if self._sigstop_released_at > 0 and time_since_sigstop < EMERGENCY_SIGSTOP_COOLDOWN_SEC:
                self.logger.warning(
                    f"[v266.1] RAM at {current_used_percent:.1f}% but SIGSTOP in cooldown "
                    f"({EMERGENCY_SIGSTOP_COOLDOWN_SEC - time_since_sigstop:.0f}s remaining)"
                )
                return

            self.logger.critical(
                f"[v266.1] LAST RESORT: RAM at {current_used_percent:.1f}% "
                f"(>={EMERGENCY_SIGSTOP_RAM_PERCENT}%) after unload + GCP failed. "
                f"One-shot SIGSTOP on JARVIS-owned PIDs."
            )
            self._sigstop_active = True
            self._sigstop_started_at = time.time()
            paused_count = await self._pause_local_llm_processes()
            self.logger.critical(f"[v266.1] SIGSTOP sent to {paused_count} process(es)")
            return

        # --- SIGSTOP timeout: terminate if no recovery ---
        if self._sigstop_active:
            sigstop_elapsed = time.time() - self._sigstop_started_at
            if sigstop_elapsed >= EMERGENCY_SIGSTOP_TIMEOUT_SEC:
                self.logger.critical(
                    f"[v266.1] SIGSTOP timeout ({sigstop_elapsed:.0f}s) — "
                    f"terminating paused processes (no cycling)"
                )
                await self._terminate_paused_processes()
                self._sigstop_active = False
                self._emergency_offload_active = False
                self._sigstop_released_at = time.time()
                self._emergency_offload_hysteresis_armed = True
                return

            # Check if GCP became available while SIGSTOP'd
            if self._gcp_controller and hasattr(self._gcp_controller, 'is_vm_available'):
                if self._gcp_controller.is_vm_available():
                    self.logger.info("[v266.1] GCP VM ready during SIGSTOP — terminating local processes")
                    await self._terminate_paused_processes()
                    self._sigstop_active = False
                    self._emergency_offload_active = False
                    return

        # --- Global timeout: force-end offload ---
        max_offload_duration = EMERGENCY_SIGSTOP_TIMEOUT_SEC * 3  # 180s absolute max
        if elapsed >= max_offload_duration:
            self.logger.warning(
                f"[v266.1] Global offload timeout ({elapsed:.0f}s) — force-ending"
            )
            if self._sigstop_active:
                await self._terminate_paused_processes()
            self._sigstop_active = False
            self._emergency_offload_active = False
            self._emergency_offload_hysteresis_armed = True
            return

    def _recovery_state(self) -> str:
        """Return normalized recovery state for observability and sidecar signals."""
        if self._recovery_in_progress or (
            self._recovery_task is not None and not self._recovery_task.done()
        ):
            return "reloading"
        if self._recovery_cooldown_until > time.time():
            return "cooldown"
        if self._model_needs_recovery:
            return "armed"
        return "idle"

    def _recovery_trend_percent_per_sec(self) -> float:
        """Estimate memory trend for stability gating (non-rising requirement)."""
        if len(self._recovery_percent_history) < 2:
            return 0.0
        start_ts, start_pct = self._recovery_percent_history[0]
        end_ts, end_pct = self._recovery_percent_history[-1]
        dt = max(0.001, end_ts - start_ts)
        return (end_pct - start_pct) / dt

    def _recovery_backoff_seconds(self, attempts_after_failure: int) -> float:
        idx = max(0, min(attempts_after_failure - 1, len(RECOVERY_BACKOFF_SCHEDULE_SEC) - 1))
        return float(RECOVERY_BACKOFF_SCHEDULE_SEC[idx])

    def _set_recovery_cooldown(self, delay_seconds: float, reason: str) -> None:
        delay = max(0.0, float(delay_seconds))
        until = time.time() + delay
        self._recovery_cooldown_until = max(self._recovery_cooldown_until, until)
        self.logger.info(
            "[v266.4] Recovery cooldown armed for %.0fs (reason=%s, attempts=%d)",
            delay,
            reason,
            self._recovery_attempts,
        )

    def _sync_local_circuit_from_handshake(self, handshake: Optional[Dict[str, Any]]) -> None:
        """Sync local circuit observability from UnifiedModelServing handshake."""
        if not handshake or not isinstance(handshake, dict):
            return
        state = str(handshake.get("circuit_state", "")).lower()
        if state:
            self._local_circuit_state = state

    def _on_local_warmup_event(self, event: Dict[str, Any]) -> None:
        """Receive background warmup lifecycle events from UnifiedModelServing."""
        handshake = event.get("handshake") if isinstance(event, dict) else None
        self._sync_local_circuit_from_handshake(handshake)
        phase = str(event.get("phase", "unknown")) if isinstance(event, dict) else "unknown"
        reason = str(event.get("reason", "unknown")) if isinstance(event, dict) else "unknown"
        generation = None
        if isinstance(handshake, dict):
            generation = handshake.get("generation")
        if phase == "recovery_success":
            self.logger.info(
                "[v278.0] Local warmup hook success (generation=%s, circuit=%s)",
                generation,
                self._local_circuit_state,
            )
        elif phase == "recovery_failed":
            self.logger.warning(
                "[v278.0] Local warmup hook failure (reason=%s, circuit=%s)",
                reason,
                self._local_circuit_state,
            )

    def _ensure_local_warmup_hook(self, model_serving: Any) -> None:
        """Register router warmup hook once to keep local/offload state aligned."""
        self._model_serving_ref = model_serving
        if self._local_warmup_hook_registered:
            return
        if not hasattr(model_serving, "register_local_warmup_hook"):
            return
        try:
            registered = model_serving.register_local_warmup_hook(self._on_local_warmup_event)
            self._local_warmup_hook_registered = bool(registered)
        except Exception as hook_err:
            self.logger.debug(f"[v278.0] Local warmup hook registration failed: {hook_err}")

    async def _run_model_recovery_attempt(
        self,
        attempt_number: int,
        current_used_percent: float,
    ) -> None:
        """Run one background recovery attempt under singleflight ownership."""
        self._recovery_in_progress = True
        self._local_circuit_state = "open"
        try:
            if current_used_percent >= EMERGENCY_UNLOAD_RAM_PERCENT:
                raise RuntimeError(
                    f"memory_critical_before_reload:{current_used_percent:.1f}%"
                )

            from backend.intelligence.unified_model_serving import get_model_serving

            model_serving = await asyncio.wait_for(get_model_serving(), timeout=10.0)
            self._ensure_local_warmup_hook(model_serving)

            recovery_result = await asyncio.wait_for(
                model_serving.recover_local_model_singleflight(
                    trigger="gcp_router_post_crisis",
                    require_armed=False,
                    respect_cooldown=False,
                ),
                timeout=180.0,
            )
            if not isinstance(recovery_result, dict):
                raise RuntimeError("invalid_local_recovery_result")

            handshake = recovery_result.get("handshake")
            if (not handshake or not isinstance(handshake, dict)) and hasattr(
                model_serving, "get_local_ready_handshake"
            ):
                handshake = model_serving.get_local_ready_handshake()
            self._sync_local_circuit_from_handshake(handshake)
            if self._local_circuit_state == "unknown":
                self._local_circuit_state = model_serving.get_local_circuit_state()

            # Abort cleanly if memory regresses to CRITICAL during or immediately after reload.
            ram_after = await self._get_ram_info_with_mb()
            if ram_after and ram_after.get("used_percent", 0.0) >= EMERGENCY_UNLOAD_RAM_PERCENT:
                raise RuntimeError(
                    f"memory_critical_during_reload:{ram_after.get('used_percent', 0.0):.1f}%"
                )

            if not recovery_result.get("ok"):
                raise RuntimeError(str(recovery_result.get("reason", "local_recovery_failed")))
            if not (
                bool(handshake and handshake.get("ready"))
                and bool(handshake and handshake.get("verified"))
                and self._local_circuit_state == "closed"
            ):
                raise RuntimeError(
                    "local_ready_handshake_unverified:"
                    f"ready={bool(handshake and handshake.get('ready'))},"
                    f"verified={bool(handshake and handshake.get('verified'))},"
                    f"circuit={self._local_circuit_state}"
                )

            self._model_needs_recovery = False
            self._recovery_attempts = 0
            self._recovery_stable_since = 0.0
            self._recovery_cooldown_until = 0.0
            self.logger.warning(
                "[v266.4] Post-crisis recovery COMPLETE "
                "(attempt=%d, local_circuit=%s)",
                attempt_number,
                self._local_circuit_state,
            )
            await self._signal_memory_pressure_to_repos(
                status="recovered",
                action=None,
                used_percent=current_used_percent,
            )
        except asyncio.CancelledError:
            raise
        except Exception as recovery_err:
            self._recovery_attempts += 1
            self._recovery_stable_since = 0.0
            self._local_circuit_state = "open"
            self.logger.warning(
                "[v266.4] Post-crisis recovery failed "
                "(attempt=%d/%d, error=%s)",
                self._recovery_attempts,
                RECOVERY_MAX_ATTEMPTS,
                recovery_err,
            )

            cooldown = self._recovery_backoff_seconds(self._recovery_attempts)
            if self._recovery_attempts >= RECOVERY_MAX_ATTEMPTS:
                cooldown = max(cooldown, RECOVERY_COOLDOWN_SEC)
                self.logger.warning(
                    "[v266.4] Recovery attempt budget exhausted; entering cooldown %.0fs",
                    cooldown,
                )
            self._set_recovery_cooldown(cooldown, reason=str(recovery_err))
        finally:
            self._recovery_in_progress = False

    async def _check_model_recovery(self, current_used_percent: float) -> None:
        """Check stability gate and singleflight launch for post-crisis reload."""
        now = time.time()
        stable_target = min(
            max(RECOVERY_STABILITY_DURATION_SEC, RECOVERY_STABILITY_MIN_SEC),
            RECOVERY_STABILITY_MAX_SEC,
        )
        trend = self._recovery_trend_percent_per_sec()
        non_rising = trend <= RECOVERY_TREND_RISE_EPSILON

        # Abort if pressure returns to CRITICAL.
        if current_used_percent >= EMERGENCY_UNLOAD_RAM_PERCENT:
            if self._recovery_stable_since > 0.0:
                self.logger.warning(
                    "[v266.4] Recovery stability window aborted: RAM returned to CRITICAL "
                    "(%.1f%%)",
                    current_used_percent,
                )
            self._recovery_stable_since = 0.0
            if self._recovery_task is not None and not self._recovery_task.done():
                self._recovery_task.cancel()
            self._recovery_in_progress = False
            self._set_recovery_cooldown(RECOVERY_BACKOFF_SCHEDULE_SEC[-1], "memory_returned_critical")
            return

        if self._recovery_in_progress or (
            self._recovery_task is not None and not self._recovery_task.done()
        ):
            return

        if self._recovery_cooldown_until > now:
            return

        if self._recovery_attempts >= RECOVERY_MAX_ATTEMPTS:
            # Keep candidate armed but avoid tight retry loops.
            if self._recovery_cooldown_until > now:
                return
            self.logger.info(
                "[v266.4] Recovery cooldown elapsed; resetting attempt budget"
            )
            self._recovery_attempts = 0
            return

        if current_used_percent <= RECOVERY_STABILITY_THRESHOLD_PERCENT and non_rising:
            if self._recovery_stable_since == 0.0:
                self._recovery_stable_since = now
                self.logger.info(
                    "[v266.4] Recovery stability gate opened "
                    "(ram=%.1f%% trend=%.3f%%/s target=%.0fs)",
                    current_used_percent,
                    trend,
                    stable_target,
                )
                return

            elapsed = now - self._recovery_stable_since
            if elapsed < stable_target:
                return

            attempt_number = self._recovery_attempts + 1
            self.logger.warning(
                "[v266.4] Recovery launch singleflight "
                "(attempt=%d/%d, ram=%.1f%%, stable=%.0fs, trend=%.3f%%/s)",
                attempt_number,
                RECOVERY_MAX_ATTEMPTS,
                current_used_percent,
                elapsed,
                trend,
            )
            self._recovery_task = asyncio.create_task(
                self._run_model_recovery_attempt(attempt_number, current_used_percent),
                name="post-crisis-model-recovery",
            )
            return

        # Reset gate when pressure is above threshold or trend is rising.
        if self._recovery_stable_since > 0.0:
            self.logger.debug(
                "[v266.4] Recovery stability reset (ram=%.1f%% trend=%.3f%%/s)",
                current_used_percent,
                trend,
            )
            self._recovery_stable_since = 0.0

    async def _release_emergency_offload(self, reason: str) -> None:
        """
        v93.0/v266.1: Release emergency offload - SIGCONT all paused processes.

        v266.1: Simplified release — no longer tracks cycle counts (the old
        cycling logic was the root cause of the death spiral).  Cooldown is
        now enforced via _sigstop_released_at in _check_offload_escalation().
        Hysteresis is still armed so that re-trigger requires a RAM drop.
        """
        if not self._emergency_offload_active:
            return

        self.logger.info(f"[v266.1] Releasing emergency offload (reason: {reason})")

        resumed_count = 0
        for pid, name in list(self._paused_processes.items()):
            if self._resume_process(pid, name):
                resumed_count += 1

        self._paused_processes.clear()
        self._emergency_offload_active = False
        self._emergency_offload_started_at = 0.0
        self._pre_unload_percent = 0.0
        self._clean_unload_fired = False
        self._clean_unload_verified = False
        self._sigstop_active = False
        self._sigstop_started_at = 0.0

        # v266.1: Record release time for SIGSTOP cooldown enforcement
        self._sigstop_released_at = time.time()
        self._emergency_offload_hysteresis_armed = True

        self.logger.info(
            f"[v266.1] SIGSTOP released, hysteresis armed, "
            f"cooldown={EMERGENCY_SIGSTOP_COOLDOWN_SEC}s starts now"
        )

        # v93.0: Signal to other repos that pressure is normal
        await self._signal_memory_pressure_to_repos(
            status="normal",
            action=None,
            used_percent=0.0,
        )

        self.logger.info(f"[v93.0] Emergency offload released - resumed {resumed_count} processes")

    def _resume_process(self, pid: int, name: str) -> bool:
        """
        v93.0/v148.0: Send SIGCONT to a process with fallback handling.
        
        v148.0 Enhancement: Added robust fallback for SIGCONT failures:
        - If process died, removes from paused list (no error)
        - If permission denied, logs warning and marks for cleanup
        - If SIGCONT fails but process exists, tries escalation
        """
        try:
            import psutil

            # Check if process exists
            if not psutil.pid_exists(pid):
                self.logger.debug(f"[v148.0] Process {pid} no longer exists - removing from paused list")
                return False  # Process died, not an error

            # Try to get process status
            try:
                proc = psutil.Process(pid)
                proc_status = proc.status()
            except psutil.NoSuchProcess:
                self.logger.debug(f"[v148.0] Process {pid} disappeared during check")
                return False
            except psutil.AccessDenied:
                self.logger.warning(f"[v148.0] Access denied checking status of PID {pid}")
                proc_status = "unknown"

            # Send SIGCONT
            try:
                os.kill(pid, signal.SIGCONT)
                self.logger.debug(f"[v93.0] SIGCONT sent to PID {pid} ({name})")
                return True
                
            except PermissionError:
                self.logger.warning(
                    f"[v148.0] Permission denied resuming PID {pid} ({name}) - "
                    f"status was: {proc_status}. Process may need manual intervention."
                )
                return False
                
            except ProcessLookupError:
                self.logger.debug(f"[v148.0] Process {pid} died before SIGCONT")
                return False
                
            except OSError as os_err:
                # v148.0: Try to understand why SIGCONT failed
                if os_err.errno == 3:  # ESRCH - No such process
                    self.logger.debug(f"[v148.0] Process {pid} no longer exists")
                    return False
                elif os_err.errno == 1:  # EPERM - Operation not permitted
                    self.logger.warning(
                        f"[v148.0] Cannot resume PID {pid} ({name}) - operation not permitted. "
                        f"Process status: {proc_status}"
                    )
                    return False
                else:
                    self.logger.warning(f"[v148.0] OS error resuming PID {pid}: {os_err}")
                    return False

        except Exception as e:
            self.logger.warning(f"[v93.0] Error resuming PID {pid}: {e}")
            return False

    async def _terminate_paused_processes(self) -> None:
        """v93.0: Terminate paused processes (when GCP ready to take over)."""
        # Signal to other repos that they should terminate
        await self._signal_memory_pressure_to_repos(
            status="offload_active",
            action="terminate",
            used_percent=0.0,
        )

        for pid, name in list(self._paused_processes.items()):
            try:
                import psutil

                if not psutil.pid_exists(pid):
                    continue

                # First resume so it can clean up
                os.kill(pid, signal.SIGCONT)
                await asyncio.sleep(0.1)

                # Then terminate
                os.kill(pid, signal.SIGTERM)
                self.logger.info(f"[v93.0] Terminated local LLM process PID {pid} ({name})")

                # Wait for graceful exit
                await asyncio.sleep(1.0)

                # Force kill if still alive
                if psutil.pid_exists(pid):
                    os.kill(pid, signal.SIGKILL)
                    self.logger.warning(f"[v93.0] Force killed PID {pid}")

            except Exception as e:
                self.logger.warning(f"[v93.0] Error terminating PID {pid}: {e}")

        self._paused_processes.clear()

    async def _terminate_local_llm_processes(self, reason: str) -> int:
        """
        v192.0: Terminate local LLM processes directly (for cycle escalation).

        Unlike _terminate_paused_processes which terminates already-paused processes,
        this method finds and terminates LLM processes directly without SIGSTOP first.
        Used when SIGSTOP/SIGCONT cycling is detected and doesn't free memory.

        Args:
            reason: Why termination is happening (for logging)

        Returns:
            Number of processes terminated
        """
        terminated_count = 0

        try:
            import psutil

            self.logger.critical(
                f"[v192.0] TERMINATING local LLM processes (reason: {reason})"
            )

            # Signal to other repos that we're terminating
            await self._signal_memory_pressure_to_repos(
                status="offload_active",
                action="terminate",
                used_percent=0.0,
            )

            # Get processes from ML loader if available
            if self._ml_loader_ref is None:
                try:
                    from backend.core.process_isolated_ml_loader import get_ml_loader
                    self._ml_loader_ref = await get_ml_loader()
                except Exception:
                    pass

            pids_to_terminate: Set[int] = set()

            if self._ml_loader_ref and hasattr(self._ml_loader_ref, '_active_processes'):
                pids_to_terminate.update(self._ml_loader_ref._active_processes.keys())

            # Scan for known LLM process patterns
            llm_patterns = [
                "ollama", "llama", "llama.cpp", "llamacpp",
                "text-generation", "vllm", "transformers",
                "jarvis-prime", "jarvis_prime",
            ]

            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    proc_info = proc.info
                    pid = proc_info['pid']

                    # Skip system processes
                    if pid <= 1:
                        continue

                    name = (proc_info.get('name') or '').lower()
                    cmdline = ' '.join(proc_info.get('cmdline') or []).lower()

                    is_llm_process = any(
                        pattern in name or pattern in cmdline
                        for pattern in llm_patterns
                    )

                    if is_llm_process:
                        pids_to_terminate.add(pid)

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Terminate each process
            for pid in pids_to_terminate:
                try:
                    if not psutil.pid_exists(pid):
                        continue

                    proc = psutil.Process(pid)
                    proc_name = proc.name()

                    # First try graceful termination
                    os.kill(pid, signal.SIGTERM)
                    self.logger.info(
                        f"[v192.0] Sent SIGTERM to LLM process PID {pid} ({proc_name})"
                    )

                    # Wait briefly for graceful exit
                    await asyncio.sleep(1.0)

                    # Force kill if still alive
                    if psutil.pid_exists(pid):
                        os.kill(pid, signal.SIGKILL)
                        self.logger.warning(f"[v192.0] Force killed PID {pid}")

                    terminated_count += 1

                except psutil.NoSuchProcess:
                    continue
                except Exception as e:
                    self.logger.warning(f"[v192.0] Error terminating PID {pid}: {e}")

            # Clear any paused process tracking
            self._paused_processes.clear()
            self._local_llm_pids.clear()

            # Force garbage collection
            try:
                import gc
                gc.collect()
                self.logger.info("[v192.0] Forced garbage collection after termination")
            except Exception:
                pass

            self.logger.info(
                f"[v192.0] Terminated {terminated_count} local LLM processes"
            )

        except Exception as e:
            self.logger.error(f"[v192.0] Error terminating LLM processes: {e}")

        return terminated_count

    async def _signal_memory_pressure_to_repos(
        self,
        status: str,
        action: Optional[str] = None,
        used_percent: float = 0.0,
        rate_mb_sec: float = 0.0,
    ) -> None:
        """
        v93.0: Signal memory pressure status to other repos via shared file.

        This allows jarvis-prime and other processes to be aware of memory pressure
        and respond appropriately (reduce load, prepare for offload, etc.)

        Args:
            status: One of "normal", "elevated", "critical", "offload_active"
            action: Optional action hint: "pause", "terminate", "reduce_load"
            used_percent: Current RAM usage percentage
            rate_mb_sec: Current memory growth rate
        """
        try:
            # Ensure cross-repo directory exists
            CROSS_REPO_DIR.mkdir(parents=True, exist_ok=True)

            signal_data = {
                "timestamp": time.time(),
                "status": status,
                "action": action,
                "used_percent": used_percent,
                "rate_mb_sec": rate_mb_sec,
                "source": "gcp_hybrid_prime_router",
                "thresholds": {
                    "gcp_trigger": GCP_TRIGGER_RAM_PERCENT,
                    "vm_provisioning": VM_PROVISIONING_THRESHOLD,
                    "emergency_offload": EMERGENCY_UNLOAD_RAM_PERCENT,
                    "critical": CRITICAL_RAM_PERCENT,
                },
                "emergency_offload_active": self._emergency_offload_active,
                "paused_processes_count": len(self._paused_processes),
                "recovery_state": self._recovery_state(),
                "recovery_attempts": self._recovery_attempts,
                "recovery_cooldown_until": self._recovery_cooldown_until,
                "recovery_stable_since": self._recovery_stable_since,
                "recovery_memory_trend_pct_per_sec": self._recovery_trend_percent_per_sec(),
                "local_circuit_state": self._local_circuit_state,
            }

            with open(MEMORY_PRESSURE_SIGNAL_FILE, "w") as f:
                json.dump(signal_data, f, indent=2)

            self.logger.debug(
                f"[v93.0] Signaled memory pressure to repos: {status} "
                f"(action={action}, RAM={used_percent:.1f}%)"
            )

        except Exception as e:
            self.logger.warning(f"[v93.0] Failed to signal memory pressure: {e}")

    async def _clear_memory_pressure_signal(self) -> None:
        """v93.0: Clear memory pressure signal when returning to normal.
        v265.7: DELETE the file instead of writing status=normal. A stale
        memory_pressure.json with status=normal still triggers Clean Slate
        detection (any existing file → confidence > 0). Deletion is the only
        way to guarantee no false recovery on next startup.
        """
        try:
            if MEMORY_PRESSURE_SIGNAL_FILE.exists():
                MEMORY_PRESSURE_SIGNAL_FILE.unlink(missing_ok=True)
                self.logger.debug(
                    "[v265.7] Deleted memory_pressure.json (pressure subsided)"
                )
        except Exception as e:
            self.logger.warning(f"[v93.0] Failed to clear memory pressure signal: {e}")

    async def _aggressive_memory_cleanup(self, memory_trend: float) -> None:
        """
        v149.0: Aggressive memory cleanup when memory grows despite LLM offload.

        This indicates non-LLM processes are consuming memory. We perform:
        1. Force garbage collection with all generations
        2. Clear Python caches (functools.lru_cache, etc.)
        3. Trigger database connection pool cleanup
        4. Notify enterprise hooks for coordinated cleanup

        Args:
            memory_trend: Memory growth percentage since offload started
        """
        import gc
        import functools

        self.logger.info(f"[v149.0] Aggressive memory cleanup triggered (trend: +{memory_trend:.1f}%)")

        cleanup_stats = {
            "gc_collected": 0,
            "caches_cleared": 0,
            "db_pools_cleaned": False,
            "enterprise_notified": False,
        }

        # Phase 1: Aggressive garbage collection
        try:
            # Collect all generations multiple times to release circular refs
            for _ in range(3):
                cleanup_stats["gc_collected"] += gc.collect(2)  # Full collection

            # Force finalization
            gc.collect()
            self.logger.debug(f"[v149.0] GC collected {cleanup_stats['gc_collected']} objects")
        except Exception as e:
            self.logger.warning(f"[v149.0] GC error: {e}")

        # Phase 2: Clear functools caches (lru_cache wrappers)
        try:
            cleared_count = 0
            # Iterate over all objects to find lru_cache wrappers
            for obj in gc.get_objects():
                if hasattr(obj, 'cache_clear') and callable(obj.cache_clear):
                    try:
                        obj.cache_clear()
                        cleared_count += 1
                    except Exception:
                        pass
            cleanup_stats["caches_cleared"] = cleared_count
            if cleared_count > 0:
                self.logger.debug(f"[v149.0] Cleared {cleared_count} LRU caches")
        except Exception as e:
            self.logger.warning(f"[v149.0] Cache clear error: {e}")

        # Phase 3: Database pool cleanup
        try:
            from intelligence.cloud_database_adapter import close_database_adapter_sync
            cleanup_stats["db_pools_cleaned"] = close_database_adapter_sync()
            if cleanup_stats["db_pools_cleaned"]:
                self.logger.debug("[v149.0] Database pools cleaned")
        except ImportError:
            pass  # Module not available
        except Exception as e:
            self.logger.warning(f"[v149.0] DB pool cleanup error: {e}")

        # Phase 4: Enterprise hooks notification
        try:
            from backend.core.enterprise_hooks import handle_memory_pressure
            import psutil
            current_percent = psutil.virtual_memory().percent
            strategy = await handle_memory_pressure(
                memory_percent=current_percent,
                trend="increasing",
                slope=memory_trend / 10.0,  # Approximate slope
            )
            cleanup_stats["enterprise_notified"] = True
            self.logger.debug(f"[v149.0] Enterprise hooks strategy: {strategy}")
        except ImportError:
            pass  # Enterprise hooks not available
        except Exception as e:
            self.logger.warning(f"[v149.0] Enterprise hooks error: {e}")

        # Final GC pass
        gc.collect()

        self.logger.info(
            f"[v149.0] Aggressive cleanup complete: "
            f"gc={cleanup_stats['gc_collected']}, caches={cleanup_stats['caches_cleared']}, "
            f"db={cleanup_stats['db_pools_cleaned']}, enterprise={cleanup_stats['enterprise_notified']}"
        )

    # =========================================================================
    # v266.0: VM Lifecycle State Machine
    # =========================================================================

    def _transition_vm_lifecycle(self, new_state: VMLifecycleState, reason: str = "") -> bool:
        """Transition the VM lifecycle state machine. Returns True if transition was valid."""
        old_state = self._vm_lifecycle_state

        # Define valid transitions
        valid_transitions = {
            VMLifecycleState.IDLE: {VMLifecycleState.TRIGGERING, VMLifecycleState.STOPPING},
            VMLifecycleState.TRIGGERING: {VMLifecycleState.PROVISIONING, VMLifecycleState.IDLE, VMLifecycleState.STOPPING},
            VMLifecycleState.PROVISIONING: {VMLifecycleState.BOOTING, VMLifecycleState.COOLING_DOWN, VMLifecycleState.STOPPING},
            VMLifecycleState.BOOTING: {VMLifecycleState.ACTIVE, VMLifecycleState.COOLING_DOWN, VMLifecycleState.STOPPING},
            VMLifecycleState.ACTIVE: {VMLifecycleState.COOLING_DOWN, VMLifecycleState.STOPPING},
            VMLifecycleState.COOLING_DOWN: {VMLifecycleState.IDLE, VMLifecycleState.TRIGGERING, VMLifecycleState.STOPPING},
            VMLifecycleState.STOPPING: {VMLifecycleState.IDLE},
        }

        # STOPPING is always reachable (session shutdown)
        if new_state == VMLifecycleState.STOPPING:
            pass  # Always allowed
        elif new_state not in valid_transitions.get(old_state, set()):
            self.logger.warning(
                f"[VMLifecycle] Invalid transition {old_state.value} -> {new_state.value} "
                f"(reason: {reason})"
            )
            return False

        self._vm_lifecycle_state = new_state
        self._vm_lifecycle_changed_at = time.time()
        self.logger.info(
            f"[VMLifecycle] {old_state.value} -> {new_state.value} (reason: {reason})"
        )

        # v266.0: Start model unload task when entering ACTIVE
        if new_state == VMLifecycleState.ACTIVE:
            if self._model_unload_task is None or self._model_unload_task.done():
                self._model_unload_task = asyncio.create_task(
                    self._unload_local_model_after_stability(),
                    name="gcp_model_unload_after_stability"
                )

        # v266.0: Cancel model unload if leaving ACTIVE
        if old_state == VMLifecycleState.ACTIVE and new_state != VMLifecycleState.ACTIVE:
            if self._model_unload_task and not self._model_unload_task.done():
                self._model_unload_task.cancel()
            os.environ.pop("JARVIS_GCP_OFFLOAD_ACTIVE", None)

        return True

    @property
    def _vm_provisioning_in_progress(self) -> bool:
        """Backward compat: True when VM is being provisioned or booting."""
        return self._vm_lifecycle_state in (
            VMLifecycleState.PROVISIONING, VMLifecycleState.BOOTING
        )

    @_vm_provisioning_in_progress.setter
    def _vm_provisioning_in_progress(self, value: bool) -> None:
        """Backward compat setter — no-op. State machine handles transitions."""
        pass  # Ignored — state machine handles this

    async def _await_vm_health_for_active(self, reason: str = "") -> None:
        """Poll GCP VM health before transitioning BOOTING → ACTIVE.

        The VM API reports RUNNING before the inference server is ready.
        This task polls is_vm_available() (which checks the /health endpoint)
        to confirm the inference stack is actually serving before we enter
        ACTIVE state. Times out after 300s → COOLING_DOWN.
        """
        health_timeout = float(os.getenv("GCP_VM_HEALTH_GATE_TIMEOUT", "300.0"))
        poll_interval = 10.0
        deadline = time.time() + health_timeout

        try:
            while time.time() < deadline:
                if self._vm_lifecycle_state != VMLifecycleState.BOOTING:
                    self.logger.info(
                        f"[VMLifecycle] Left BOOTING state ({self._vm_lifecycle_state.value}), "
                        f"aborting health gate"
                    )
                    return

                # Check if inference server is healthy
                if self._gcp_controller and hasattr(self._gcp_controller, 'is_vm_available'):
                    if self._gcp_controller.is_vm_available():
                        self._transition_vm_lifecycle(
                            VMLifecycleState.ACTIVE, "health_check_passed"
                        )
                        self.logger.info(
                            f"[VMLifecycle] VM inference health confirmed — ACTIVE (reason: {reason})"
                        )
                        return

                await asyncio.sleep(poll_interval)

            # Timeout — VM never became healthy
            self.logger.warning(
                f"[VMLifecycle] VM health gate timeout ({health_timeout}s) — "
                f"inference server never confirmed healthy"
            )
            self._transition_vm_lifecycle(
                VMLifecycleState.COOLING_DOWN, "health_gate_timeout"
            )
            self._cooling_started_at = time.time()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"[VMLifecycle] Health gate error: {e}")
            if self._vm_lifecycle_state == VMLifecycleState.BOOTING:
                self._transition_vm_lifecycle(
                    VMLifecycleState.COOLING_DOWN, f"health_gate_error: {e}"
                )
                self._cooling_started_at = time.time()

    async def _unload_local_model_after_stability(self) -> None:
        """After GCP VM proves stable, unload local model to reclaim RAM."""
        try:
            self._active_stability_count = 0

            for _ in range(GCP_ACTIVE_STABILITY_CHECKS):
                await asyncio.sleep(10.0)

                # Check VM is still ACTIVE and healthy
                if self._vm_lifecycle_state != VMLifecycleState.ACTIVE:
                    self.logger.info("[VMLifecycle] Left ACTIVE state, aborting model unload")
                    return

                if self._gcp_controller and hasattr(self._gcp_controller, 'is_vm_available'):
                    if not self._gcp_controller.is_vm_available():
                        self.logger.info("[VMLifecycle] GCP VM unhealthy, aborting model unload")
                        return

                self._active_stability_count += 1

            # GCP stable for N checks — unload local model
            self.logger.info(
                f"[VMLifecycle] GCP VM stable for {GCP_ACTIVE_STABILITY_CHECKS * 10}s, "
                f"unloading local model to reclaim RAM"
            )

            try:
                from backend.intelligence.unified_model_serving import get_model_serving
                model_serving = get_model_serving()
                if model_serving and hasattr(model_serving, 'stop'):
                    await model_serving.stop()
                    self.logger.info("[VMLifecycle] Local model unloaded — RAM reclaimed")
                    os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"
                else:
                    self.logger.debug("[VMLifecycle] UnifiedModelServing not available or has no stop()")
            except ImportError:
                self.logger.debug("UnifiedModelServing not available for unload")
            except Exception as e:
                self.logger.warning(f"[VMLifecycle] Local model unload failed: {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug(f"[VMLifecycle] Model unload task error: {e}")

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
        # v266.0: State machine handles guard
        if self._vm_lifecycle_state in (VMLifecycleState.PROVISIONING, VMLifecycleState.BOOTING, VMLifecycleState.ACTIVE):
            self.logger.debug(f"VM lifecycle in {self._vm_lifecycle_state.value}, skipping provision")
            return False

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
                self._gcp_permanently_unavailable = True
                return False

            # v92.0: Check if GCP is configured and enabled BEFORE attempting
            if hasattr(self._gcp_controller, 'config'):
                config = self._gcp_controller.config
                if hasattr(config, 'is_valid_for_vm_operations'):
                    is_valid, error_msg = config.is_valid_for_vm_operations()
                    if not is_valid:
                        # GCP is permanently unavailable (disabled or misconfigured)
                        if not self._gcp_permanently_unavailable:
                            self.logger.info(
                                f"GCP VM provisioning disabled: {error_msg}. "
                                f"Memory pressure monitoring will continue but VM provisioning skipped."
                            )
                        self._gcp_permanently_unavailable = True
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

                    # v266.0: Transition to BOOTING — ACTIVE deferred until health check
                    self._transition_vm_lifecycle(VMLifecycleState.BOOTING, "vm_created")
                    # Launch background task to poll health and transition to ACTIVE
                    asyncio.create_task(
                        self._await_vm_health_for_active(reason=reason),
                        name="gcp_vm_health_gate"
                    )

                    # v148.1: Record success for circuit breaker
                    if ENTERPRISE_HOOKS_AVAILABLE and record_provider_success:
                        try:
                            record_provider_success("llm_inference", "gcp_vm")
                        except Exception:
                            pass

                    # v148.1: Update health to healthy
                    if ENTERPRISE_HOOKS_AVAILABLE and update_component_health and HealthStatus:
                        try:
                            update_component_health(
                                "gcp_hybrid_router",
                                HealthStatus.HEALTHY,
                                message="GCP VM provisioned successfully",
                            )
                        except Exception:
                            pass

                    return True
                else:
                    # v153.0: Use RecoveryCascadeManager for intelligent failure handling
                    error = RuntimeError(f"GCP VM provisioning failed for reason: {reason}")
                    failure_type = self._recovery_cascade.classify_failure(error, reason)
                    recovery_state = self._recovery_cascade.record_failure(failure_type, error)

                    # Check if this is a permanent failure (disabled/misconfigured)
                    if hasattr(self._gcp_controller, 'config'):
                        config = self._gcp_controller.config
                        if hasattr(config, 'is_valid_for_vm_operations'):
                            is_valid, _ = config.is_valid_for_vm_operations()
                            if not is_valid:
                                self._gcp_permanently_unavailable = True
                                # Also mark as permanent in recovery cascade
                                recovery_state.permanently_disabled = True
                                recovery_state.disable_reason = "GCP not configured for VM operations"

                    # v153.0: Determine fallback tier based on failure type
                    if not recovery_state.permanently_disabled:
                        fallback_tier, fallback_reason = self._recovery_cascade.get_fallback_tier(
                            prefer_local=(failure_type == GCPFailureType.OOM_RECOVERY)
                        )
                        self.logger.info(
                            f"[v153.0] GCP VM provisioning failed ({failure_type.value}), "
                            f"falling back to {fallback_tier.value}: {fallback_reason}"
                        )
                        self._degradation_mode = True
                        self._degradation_reason = f"GCP failure: {failure_type.value}"
                        self._last_successful_tier = fallback_tier
                    else:
                        self.logger.warning(
                            f"[v153.0] GCP permanently unavailable: {recovery_state.disable_reason}"
                        )

                    # v148.1: Report failure to enterprise recovery engine
                    if ENTERPRISE_HOOKS_AVAILABLE and handle_gcp_failure and GCPErrorContext:
                        try:
                            ctx = GCPErrorContext(
                                error=error,
                                error_message=str(error),
                                component="gcp_vm",
                            )
                            strategy = await handle_gcp_failure(error, ctx)
                            self.logger.info(f"[v148.1] GCP failure handled, strategy: {strategy.value}")
                        except Exception as e:
                            self.logger.debug(f"[v148.1] GCP failure handler error: {e}")

                    # v148.1: Record provider failure for circuit breaker
                    if ENTERPRISE_HOOKS_AVAILABLE and record_provider_failure:
                        try:
                            record_provider_failure(
                                "llm_inference",
                                "gcp_vm",
                                error,
                            )
                        except Exception:
                            pass

                    self.logger.error(
                        f"GCP VM provisioning failed (type={failure_type.value}, "
                        f"cooldown={recovery_state.cooldown_until - time.time():.1f}s)"
                    )
                    return False
            else:
                self.logger.warning("GCP controller doesn't support create_vm()")
                return False

        except Exception as e:
            self.logger.error(f"VM provisioning error: {e}")
            return False

        finally:
            # v266.0: State transitions handled by success/failure paths above
            pass  # self._vm_provisioning_in_progress is now a derived property
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

        # v266.0: Transition to STOPPING state
        if self._vm_lifecycle_state != VMLifecycleState.IDLE:
            self._transition_vm_lifecycle(VMLifecycleState.STOPPING, "router_shutdown")

        # v93.0: Release emergency offload FIRST to ensure paused processes resume
        if self._emergency_offload_active:
            self.logger.info("[v93.0] Releasing emergency offload during shutdown...")
            await self._release_emergency_offload(reason="router_shutdown")

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

        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass

        if (
            self._memory_quantizer is not None
            and self._memory_tier_change_callback is not None
            and hasattr(self._memory_quantizer, "unregister_tier_change_callback")
        ):
            try:
                self._memory_quantizer.unregister_tier_change_callback(
                    self._memory_tier_change_callback
                )
            except Exception as callback_err:
                self.logger.debug(
                    f"[StartupContract] Memory callback unregister failed: {callback_err}"
                )
        self._memory_quantizer = None
        self._memory_tier_change_callback = None
        self._set_startup_contract_state(
            router_started=False,
            memory_callbacks_registered=False,
            reason="stopped",
        )

        if (
            self._local_warmup_hook_registered
            and self._model_serving_ref is not None
            and hasattr(self._model_serving_ref, "unregister_local_warmup_hook")
        ):
            try:
                self._model_serving_ref.unregister_local_warmup_hook(
                    self._on_local_warmup_event
                )
            except Exception as hook_err:
                self.logger.debug(f"[v278.0] Local warmup hook unregister failed: {hook_err}")
        self._local_warmup_hook_registered = False
        self._model_serving_ref = None

        self.logger.info(
            f"GCPHybridPrimeRouter stopped "
            f"(total: {self._metrics.total_requests}, "
            f"cost: ${self._metrics.total_cost:.4f})"
        )

    async def _connect_integrations(self) -> None:
        """
        Connect to external integrations.

        v95.19: Enhanced with internal operation timeouts to prevent phase blocking.
        Uses non-blocking acquisition for singletons that may be initializing in other phases.
        """
        # v95.19: Internal operation timeout (shorter than phase timeout)
        op_timeout = float(os.getenv("GCP_ROUTER_OP_TIMEOUT", "5.0"))

        # Cross-repo cost sync (with timeout)
        try:
            from backend.core.cross_repo_cost_sync import get_cross_repo_cost_sync
            self._cost_sync = await asyncio.wait_for(
                get_cross_repo_cost_sync("jarvis"),
                timeout=op_timeout,
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"CrossRepoCostSync connection timed out ({op_timeout}s)")
        except Exception as e:
            self.logger.warning(f"CrossRepoCostSync not available: {e}")

        # Cross-repo neural mesh (with non-blocking timeout)
        # v95.19: Use timeout to avoid blocking if Phase 13 is still initializing
        try:
            from backend.core.registry.cross_repo_neural_mesh import get_cross_repo_neural_mesh
            self._neural_mesh = await get_cross_repo_neural_mesh(
                timeout=op_timeout,
                create_if_missing=False,  # Don't create - Phase 13 should do that
            )
            if self._neural_mesh is None:
                self.logger.info("Neural Mesh not yet ready - will retry later")
        except asyncio.TimeoutError:
            self.logger.warning(f"CrossRepoNeuralMesh connection timed out ({op_timeout}s)")
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

        # v266.0: If local model is being swapped (thrash cascade), use cloud.
        # _model_swapping lives on PrimeLocalClient, not UnifiedModelServing.
        try:
            from backend.intelligence.unified_model_serving import (
                _model_serving as _ums_singleton,
                ModelProvider as _UMSProvider,
            )
            if _ums_singleton:
                _local_client = getattr(_ums_singleton, '_clients', {}).get(
                    _UMSProvider.PRIME_LOCAL
                )
                if _local_client and getattr(_local_client, '_model_swapping', False):
                    self.logger.info(
                        "[v266.0] Local model swapping (thrash cascade) — routing to cloud"
                    )
                    return RoutingDecision(
                        tier=RoutingTier.CLOUD_CLAUDE,
                        reason=RoutingReason.CAPABILITY_REQUIRED,
                        timeout_ms=CLOUD_API_TIMEOUT_MS,
                        metadata={"model_swapping": True},
                    )
        except (ImportError, AttributeError):
            pass

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
        """Get router metrics (v93.0 extended with Predictive Memory Defense)."""
        # Get circuit breaker status
        cb_status = {}
        if self._circuit_breaker:
            for tier in RoutingTier:
                health = self._circuit_breaker.get_tier_health(tier.value)
                if health:
                    cb_status[tier.value] = {
                        "state": health.state.value,
                        "failure_count": health.consecutive_failures,
                        "success_rate": health.success_rate,
                    }
        else:
            # Legacy circuit breakers
            cb_status = {
                tier.value: cb["state"]
                for tier, cb in self._legacy_circuit_breakers.items()
            }

        # v93.0: Get current RAM info if available
        current_ram_info = {}
        if self._cached_ram_info:
            current_ram_info = {
                "used_percent": self._cached_ram_info.get("used_percent", 0),
                "available_gb": self._cached_ram_info.get("available_gb", 0),
                "used_mb": self._cached_ram_info.get("used_mb", 0),
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
            "post_crisis_recovery": {
                "state": self._recovery_state(),
                "needs_recovery": self._model_needs_recovery,
                "attempts": self._recovery_attempts,
                "cooldown_until": self._recovery_cooldown_until,
                "stable_since": self._recovery_stable_since,
                "local_circuit_state": self._local_circuit_state,
                "trend_pct_per_sec": self._recovery_trend_percent_per_sec(),
            },
            # v93.0: Predictive Memory Defense metrics
            "predictive_defense": {
                "enabled": True,
                "thresholds": {
                    "gcp_trigger_percent": GCP_TRIGGER_RAM_PERCENT,
                    "vm_provisioning_percent": VM_PROVISIONING_THRESHOLD,
                    "emergency_offload_percent": EMERGENCY_UNLOAD_RAM_PERCENT,
                    "critical_percent": CRITICAL_RAM_PERCENT,
                    "adaptive_poll_threshold_percent": MEMORY_ADAPTIVE_POLL_THRESHOLD,
                    "spike_rate_threshold_mb_sec": MEMORY_SPIKE_RATE_THRESHOLD_MB,
                },
                "current_state": {
                    "ram": current_ram_info,
                    "memory_rate_mb_sec": self._current_memory_rate_mb_sec,
                    "in_fast_polling_mode": self._in_fast_polling_mode,
                    "current_poll_interval_sec": self._current_poll_interval,
                    "emergency_offload_active": self._emergency_offload_active,
                    "paused_processes_count": len(self._paused_processes),
                    "paused_pids": list(self._paused_processes.keys()),
                },
                "history": {
                    "memory_samples": len(self._memory_history),
                    "pressure_consecutive_failures": self._pressure_consecutive_failures,
                    "gcp_permanently_unavailable": self._gcp_permanently_unavailable,
                },
            },
            "startup_contract": self.get_startup_contract_status(),
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


async def get_gcp_hybrid_prime_router_with_contract(
    timeout_seconds: Optional[float] = None,
) -> Tuple[GCPHybridPrimeRouter, Dict[str, Any]]:
    """Get router and wait for startup contract readiness status."""
    router = await get_gcp_hybrid_prime_router()
    if timeout_seconds is None:
        try:
            timeout_seconds = float(
                os.getenv("JARVIS_ROUTER_STARTUP_CONTRACT_TIMEOUT", "20.0")
            )
        except (TypeError, ValueError):
            timeout_seconds = 20.0
    status = await router.wait_for_startup_contract(timeout_seconds=max(0.0, timeout_seconds))
    return router, status


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
    "get_gcp_hybrid_prime_router_with_contract",
    "shutdown_gcp_hybrid_prime_router",
]
