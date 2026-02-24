"""
JARVIS Recovery Policy Registry v1.0
=====================================
Centralizes circuit breaker thresholds, recovery windows, and cooldown
periods that are currently scattered across 6+ files with conflicting values.

Root cause cured:
  - PrimeRouter threshold=2 was TOO aggressive (1 timeout opens circuit)
  - Quota cooldown (300s) vs circuit recovery (30s) caused retry storms
  - 4+ independent circuit breakers with different thresholds for same resource

This module provides ONE source of truth for all recovery parameters.
Consumers read from here at initialization. Env vars override for ops tuning.

v270.4: Created as part of Phase 7 — distributed recovery unification.
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class RecoveryParams:
    """Immutable recovery parameters for a named circuit/resource."""
    resource_name: str
    description: str

    # Circuit breaker
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0
    circuit_half_open_max_calls: int = 1

    # Cooldown (after quota exhaustion / complete failure)
    cooldown_seconds: float = 60.0

    # Retry
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0

    def effective_recovery(self) -> float:
        """Return the longer of recovery_seconds and cooldown, if cooldown active."""
        return max(self.circuit_recovery_seconds, self.cooldown_seconds)


# =========================================================================
# CANONICAL RECOVERY POLICIES
# =========================================================================
# Env var names match what's already used in the codebase for backward compat.

RECOVERY_POLICIES: Dict[str, RecoveryParams] = {
    # PrimeRouter local/GCP endpoint (was threshold=2, TOO aggressive → now 3)
    "prime_router": RecoveryParams(
        resource_name="prime_router",
        description="PrimeRouter inference endpoint circuit breaker",
        circuit_failure_threshold=_env_int("PRIME_ROUTER_CIRCUIT_FAILURES", 3),
        circuit_recovery_seconds=_env_float("PRIME_ROUTER_CIRCUIT_RECOVERY", 30.0),
        circuit_half_open_max_calls=1,
        cooldown_seconds=60.0,
    ),

    # PrimeClient remote API (was threshold=5, recovery=30s — keep)
    "prime_client": RecoveryParams(
        resource_name="prime_client",
        description="PrimeClient remote API circuit breaker",
        circuit_failure_threshold=_env_int("PRIME_CIRCUIT_FAILURE_THRESHOLD", 5),
        circuit_recovery_seconds=_env_float("PRIME_CIRCUIT_RESET_TIMEOUT", 30.0),
        circuit_half_open_max_calls=_env_int("PRIME_CIRCUIT_HALF_OPEN_REQUESTS", 3),
        cooldown_seconds=60.0,
        max_retries=3,
        retry_base_delay=0.5,
        retry_max_delay=10.0,
    ),

    # UnifiedModelServing per-provider (was threshold=3, recovery=30s — keep)
    "model_serving": RecoveryParams(
        resource_name="model_serving",
        description="UnifiedModelServing per-provider circuit breaker",
        circuit_failure_threshold=_env_int("CIRCUIT_BREAKER_FAILURES", 3),
        circuit_recovery_seconds=_env_float("CIRCUIT_BREAKER_RECOVERY", 30.0),
        circuit_half_open_max_calls=1,
        cooldown_seconds=30.0,
    ),

    # GCP VM create/delete operations (threshold=3, recovery=60s — keep)
    "gcp_vm_ops": RecoveryParams(
        resource_name="gcp_vm_ops",
        description="GCP Compute Engine VM operations circuit breaker",
        circuit_failure_threshold=_env_int("GCP_CIRCUIT_FAILURE_THRESHOLD", 3),
        circuit_recovery_seconds=_env_float("GCP_CIRCUIT_RECOVERY_TIMEOUT", 60.0),
        circuit_half_open_max_calls=1,
        # Quota cooldown MUST exceed circuit recovery to prevent retry storms
        cooldown_seconds=_env_float("GCP_QUOTA_COOLDOWN_SECONDS", 300.0),
        max_retries=3,
        retry_base_delay=2.0,
        retry_max_delay=30.0,
    ),

    # GCP cost tracker (non-critical, more lenient)
    "gcp_cost_tracker": RecoveryParams(
        resource_name="gcp_cost_tracker",
        description="GCP cost tracking circuit breaker (non-critical)",
        circuit_failure_threshold=_env_int("GCP_COST_CIRCUIT_FAILURES", 5),
        circuit_recovery_seconds=_env_float("GCP_COST_CIRCUIT_RECOVERY", 30.0),
        circuit_half_open_max_calls=1,
        cooldown_seconds=30.0,
    ),

    # GCP quota check
    "gcp_quota_check": RecoveryParams(
        resource_name="gcp_quota_check",
        description="GCP quota check circuit breaker",
        circuit_failure_threshold=_env_int("GCP_QUOTA_CIRCUIT_FAILURES", 3),
        circuit_recovery_seconds=_env_float("GCP_QUOTA_CIRCUIT_RECOVERY", 60.0),
        circuit_half_open_max_calls=1,
        cooldown_seconds=60.0,
    ),

    # Cloud SQL proxy
    "cloud_sql": RecoveryParams(
        resource_name="cloud_sql",
        description="Cloud SQL proxy connection circuit breaker",
        circuit_failure_threshold=3,
        circuit_recovery_seconds=60.0,
        circuit_half_open_max_calls=1,
        cooldown_seconds=120.0,
        max_retries=3,
        retry_base_delay=1.0,
        retry_max_delay=15.0,
    ),

    # Redis (distributed locks, caching)
    "redis": RecoveryParams(
        resource_name="redis",
        description="Redis connection circuit breaker",
        circuit_failure_threshold=3,
        circuit_recovery_seconds=30.0,
        circuit_half_open_max_calls=1,
        cooldown_seconds=300.0,  # Match existing 300s cache cooldown
        max_retries=2,
        retry_base_delay=0.1,
        retry_max_delay=5.0,
    ),
}


def get_recovery_params(resource_name: str) -> Optional[RecoveryParams]:
    """Get the canonical recovery parameters for a resource.

    Returns None if no policy is registered for that resource name.
    """
    return RECOVERY_POLICIES.get(resource_name)
