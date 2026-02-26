"""
JARVIS State Authority Registry v1.0
=====================================
Declares the single authoritative source for each cross-module state concept
and provides runtime consistency validation.

This is NOT a state store — it does NOT own or cache state values.
It is a declaration layer + validation utility that detects when multiple
representations of the same concept diverge.

Root cause cured:
  - GCP VM readiness has 4 sources (bool, IP string, env var, asyncio.Event)
    with no declaration of which is canonical
  - PrimeRouter promote/demote can leave env vars and in-memory bools out of sync
  - Startup mode has 3 env vars that can drift apart

v271.0: Created as part of Phase 8 — state authority and decision auditing.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ===========================================================================
# Shared Startup Admission Gate
# ===========================================================================

_STARTUP_MODE_ORDER = (
    "local_full",
    "local_optimized",
    "sequential",
    "cloud_first",
    "cloud_only",
    "minimal",
)
_STARTUP_MODE_SET = set(_STARTUP_MODE_ORDER)
_HEAVY_BLOCKED_MODES = {"sequential", "cloud_only", "minimal"}


def normalize_startup_mode(mode: Optional[str], default: str = "local_full") -> str:
    """Normalize startup mode token to the canonical value."""
    candidate = (mode or "").strip().lower()
    if candidate in _STARTUP_MODE_SET:
        return candidate
    return default


def get_effective_startup_mode(default: str = "local_full") -> str:
    """
    Resolve authoritative effective startup mode from env.

    Source priority:
      1. JARVIS_STARTUP_EFFECTIVE_MODE
      2. JARVIS_STARTUP_MEMORY_MODE
      3. default
    """
    effective = os.environ.get("JARVIS_STARTUP_EFFECTIVE_MODE", "")
    if effective:
        return normalize_startup_mode(effective, default=default)
    memory_mode = os.environ.get("JARVIS_STARTUP_MEMORY_MODE", "")
    return normalize_startup_mode(memory_mode, default=default)


def can_spawn_heavy(
    *,
    min_available_gb: float = 1.5,
    mode: Optional[str] = None,
    available_gb: Optional[float] = None,
) -> "ConsistencyResult":
    """
    Shared heavy-init admission gate for supervisor and parallel initializer.

    Returns a ConsistencyResult where:
      - concept is "heavy_spawn_admission"
      - authoritative_value is "true"/"false" (gate open/closed)
      - divergences carries structured detail:
          [0] effective_mode=...
          [1] reason=...
          [2] available_gb=... (if known)
    """
    effective_mode = normalize_startup_mode(mode or get_effective_startup_mode())
    reason = "admitted"

    if os.environ.get("JARVIS_BACKEND_MINIMAL", "").strip().lower() == "true":
        reason = "backend_minimal=true"
        admitted = False
    elif effective_mode in _HEAVY_BLOCKED_MODES:
        reason = f"mode={effective_mode}"
        admitted = False
    else:
        admitted = True
        observed_gb: Optional[float] = available_gb
        if observed_gb is None:
            try:
                import psutil

                observed_gb = float(psutil.virtual_memory().available / (1024 ** 3))
            except Exception:
                observed_gb = None
        if observed_gb is not None and observed_gb < max(0.1, float(min_available_gb)):
            admitted = False
            reason = f"available_gb={observed_gb:.2f}<{min_available_gb:.2f}"
        available_gb = observed_gb

    details = [f"effective_mode={effective_mode}", f"reason={reason}"]
    if available_gb is not None:
        details.append(f"available_gb={available_gb:.2f}")

    return ConsistencyResult(
        concept="heavy_spawn_admission",
        consistent=admitted,
        authoritative_value="true" if admitted else "false",
        divergences=details,
    )


def publish_heavy_spawn_admission(
    *,
    context: str,
    min_available_gb: float = 1.5,
    mode: Optional[str] = None,
    available_gb: Optional[float] = None,
) -> "ConsistencyResult":
    """
    Evaluate and publish heavy-init admission state to env for cross-module use.
    """
    result = can_spawn_heavy(
        min_available_gb=min_available_gb,
        mode=mode,
        available_gb=available_gb,
    )
    effective_mode = get_effective_startup_mode()
    reason = "unknown"
    measured = ""
    for item in result.divergences:
        if item.startswith("effective_mode="):
            effective_mode = normalize_startup_mode(
                item.split("=", 1)[1],
                default=effective_mode,
            )
        elif item.startswith("reason="):
            reason = item.split("=", 1)[1]
        elif item.startswith("available_gb="):
            measured = item.split("=", 1)[1]

    os.environ["JARVIS_STARTUP_EFFECTIVE_MODE"] = effective_mode
    os.environ["JARVIS_CAN_SPAWN_HEAVY"] = (
        "true" if result.authoritative_value == "true" else "false"
    )
    os.environ["JARVIS_HEAVY_ADMISSION_REASON"] = reason
    os.environ["JARVIS_HEAVY_ADMISSION_CONTEXT"] = context
    if measured:
        os.environ["JARVIS_HEAVY_ADMISSION_AVAILABLE_GB"] = measured
    elif "JARVIS_HEAVY_ADMISSION_AVAILABLE_GB" in os.environ:
        del os.environ["JARVIS_HEAVY_ADMISSION_AVAILABLE_GB"]
    return result


# ===========================================================================
# Data Model
# ===========================================================================

@dataclass(frozen=True)
class StateDeclaration:
    """Declares the authoritative source for a state concept."""

    concept: str               # e.g., "gcp_vm_readiness"
    description: str
    authoritative_source: str  # e.g., "supervisor._invincible_node_ready"
    secondary_sources: tuple   # e.g., ("env:JARVIS_INVINCIBLE_NODE_IP", ...)
    version: str = "1.0.0"


@dataclass
class ConsistencyResult:
    """Result of a single state concept consistency check."""

    concept: str
    consistent: bool
    authoritative_value: Optional[str]
    divergences: List[str]     # Human-readable description of each divergence
    checked_at: float = 0.0

    def __post_init__(self):
        if self.checked_at == 0.0:
            self.checked_at = time.time()


# ===========================================================================
# Canonical State Declarations
# ===========================================================================

STATE_DECLARATIONS: Dict[str, StateDeclaration] = {
    "gcp_vm_readiness": StateDeclaration(
        concept="gcp_vm_readiness",
        description="Whether the GCP Invincible Node VM is ready and routable",
        authoritative_source="supervisor._invincible_node_ready",
        secondary_sources=(
            "supervisor._invincible_node_ip",
            "env:JARVIS_INVINCIBLE_NODE_IP",
            "env:JARVIS_HOLLOW_CLIENT_ACTIVE",
        ),
    ),
    "prime_routing_mode": StateDeclaration(
        concept="prime_routing_mode",
        description="Whether PrimeRouter is routing to GCP, local, or cloud",
        authoritative_source="prime_router._gcp_promoted",
        secondary_sources=(
            "prime_router._gcp_host",
            "env:JARVIS_INVINCIBLE_NODE_IP",
        ),
    ),
    "startup_memory_mode": StateDeclaration(
        concept="startup_memory_mode",
        description="Effective startup memory mode (monotonic degradation during startup)",
        authoritative_source="env:JARVIS_STARTUP_MEMORY_MODE",
        secondary_sources=(
            "env:JARVIS_STARTUP_EFFECTIVE_MODE",
            "env:JARVIS_STARTUP_DESIRED_MODE",
        ),
    ),
    "heavy_spawn_admission": StateDeclaration(
        concept="heavy_spawn_admission",
        description="Shared heavy component spawn admission gate",
        authoritative_source="env:JARVIS_CAN_SPAWN_HEAVY",
        secondary_sources=(
            "env:JARVIS_STARTUP_EFFECTIVE_MODE",
            "env:JARVIS_HEAVY_ADMISSION_REASON",
            "env:JARVIS_HEAVY_ADMISSION_AVAILABLE_GB",
        ),
    ),
}


# ===========================================================================
# Query API
# ===========================================================================

def get_state_declaration(concept: str) -> Optional[StateDeclaration]:
    """Get the declaration for a state concept."""
    return STATE_DECLARATIONS.get(concept)


# ===========================================================================
# Validator Implementations
# ===========================================================================

def _validate_gcp_vm_readiness(
    supervisor: Optional[Any] = None,
    **kwargs: Any,
) -> ConsistencyResult:
    """
    Check consistency of GCP VM readiness across all representations.

    Authoritative: supervisor._invincible_node_ready (bool)
    Secondary:
      - supervisor._invincible_node_ip (should be non-None iff ready=True)
      - env:JARVIS_INVINCIBLE_NODE_IP (should be set iff ready=True)
      - env:JARVIS_HOLLOW_CLIENT_ACTIVE (should be "true" iff ready=True)
    """
    concept = "gcp_vm_readiness"
    divergences: List[str] = []

    if supervisor is None:
        return ConsistencyResult(
            concept=concept,
            consistent=True,
            authoritative_value=None,
            divergences=["skipped: supervisor not provided"],
        )

    ready = getattr(supervisor, "_invincible_node_ready", None)
    ip = getattr(supervisor, "_invincible_node_ip", None)
    env_ip = os.environ.get("JARVIS_INVINCIBLE_NODE_IP", "")
    env_hollow = os.environ.get("JARVIS_HOLLOW_CLIENT_ACTIVE", "").lower()

    if ready is None:
        return ConsistencyResult(
            concept=concept,
            consistent=True,
            authoritative_value=None,
            divergences=["skipped: supervisor._invincible_node_ready not found"],
        )

    auth_value = str(ready)

    # Check: if ready=True, IP must be set
    if ready and not ip:
        divergences.append(
            f"ready=True but _invincible_node_ip is None/empty"
        )

    # Check: if ready=False, IP should not be set (stale)
    if not ready and ip:
        divergences.append(
            f"ready=False but _invincible_node_ip='{ip}' (stale)"
        )

    # Check: env var should agree with in-memory
    if ready and not env_ip:
        divergences.append(
            "ready=True but JARVIS_INVINCIBLE_NODE_IP env var not set"
        )
    if not ready and env_ip:
        divergences.append(
            f"ready=False but JARVIS_INVINCIBLE_NODE_IP='{env_ip}' (stale env var)"
        )

    # Check: hollow client active should agree
    if ready and env_hollow != "true":
        divergences.append(
            f"ready=True but JARVIS_HOLLOW_CLIENT_ACTIVE='{env_hollow}' (expected 'true')"
        )
    if not ready and env_hollow == "true":
        divergences.append(
            "ready=False but JARVIS_HOLLOW_CLIENT_ACTIVE='true' (stale)"
        )

    return ConsistencyResult(
        concept=concept,
        consistent=len(divergences) == 0,
        authoritative_value=auth_value,
        divergences=divergences,
    )


def _validate_prime_routing_mode(
    prime_router: Optional[Any] = None,
    **kwargs: Any,
) -> ConsistencyResult:
    """
    Check consistency of PrimeRouter routing mode.

    Authoritative: prime_router._gcp_promoted (bool)
    Secondary:
      - prime_router._gcp_host (should be non-None iff promoted=True)
      - env:JARVIS_INVINCIBLE_NODE_IP (should agree with _gcp_host)
    """
    concept = "prime_routing_mode"
    divergences: List[str] = []

    if prime_router is None:
        return ConsistencyResult(
            concept=concept,
            consistent=True,
            authoritative_value=None,
            divergences=["skipped: prime_router not provided"],
        )

    promoted = getattr(prime_router, "_gcp_promoted", None)
    gcp_host = getattr(prime_router, "_gcp_host", None)
    env_ip = os.environ.get("JARVIS_INVINCIBLE_NODE_IP", "")

    if promoted is None:
        return ConsistencyResult(
            concept=concept,
            consistent=True,
            authoritative_value=None,
            divergences=["skipped: prime_router._gcp_promoted not found"],
        )

    auth_value = str(promoted)

    # Check: if promoted=True, host must be set
    if promoted and not gcp_host:
        divergences.append(
            "promoted=True but _gcp_host is None/empty"
        )

    # Check: if promoted=False, host should be cleared
    if not promoted and gcp_host:
        divergences.append(
            f"promoted=False but _gcp_host='{gcp_host}' (stale)"
        )

    # Check: env var should agree when promoted
    if promoted and gcp_host and env_ip and env_ip != gcp_host:
        divergences.append(
            f"promoted to '{gcp_host}' but JARVIS_INVINCIBLE_NODE_IP='{env_ip}' (mismatch)"
        )

    return ConsistencyResult(
        concept=concept,
        consistent=len(divergences) == 0,
        authoritative_value=auth_value,
        divergences=divergences,
    )


def _validate_startup_memory_mode(**kwargs: Any) -> ConsistencyResult:
    """
    Check consistency of startup memory mode env vars.

    Authoritative: JARVIS_STARTUP_MEMORY_MODE
    Secondary:
      - JARVIS_STARTUP_EFFECTIVE_MODE (should equal authoritative)
      - JARVIS_STARTUP_DESIRED_MODE (informational — divergence is expected
        when degradation happened, reported as INFO not warning)
    """
    concept = "startup_memory_mode"
    divergences: List[str] = []

    mode = os.environ.get("JARVIS_STARTUP_MEMORY_MODE", "")
    effective = os.environ.get("JARVIS_STARTUP_EFFECTIVE_MODE", "")

    if not mode:
        return ConsistencyResult(
            concept=concept,
            consistent=True,
            authoritative_value=None,
            divergences=["skipped: JARVIS_STARTUP_MEMORY_MODE not set (pre-startup)"],
        )

    # effective MUST match authoritative mode
    if effective and effective != mode:
        divergences.append(
            f"MEMORY_MODE='{mode}' but EFFECTIVE_MODE='{effective}' (must match)"
        )

    # desired divergence is informational (degradation happened), not an error
    # We still report it but it doesn't break consistency
    # (The authoritative mode IS the effective mode, desired is the original intent)

    return ConsistencyResult(
        concept=concept,
        consistent=len(divergences) == 0,
        authoritative_value=mode,
        divergences=divergences,
    )


def _validate_heavy_spawn_admission(**kwargs: Any) -> ConsistencyResult:
    """Validate consistency between effective mode and heavy spawn gate env."""
    concept = "heavy_spawn_admission"
    divergences: List[str] = []

    gate_raw = os.environ.get("JARVIS_CAN_SPAWN_HEAVY", "").strip().lower()
    mode = get_effective_startup_mode()
    backend_minimal = os.environ.get("JARVIS_BACKEND_MINIMAL", "").strip().lower()

    if not gate_raw:
        return ConsistencyResult(
            concept=concept,
            consistent=True,
            authoritative_value=None,
            divergences=["skipped: JARVIS_CAN_SPAWN_HEAVY not set"],
        )

    if gate_raw not in {"true", "false"}:
        divergences.append(
            f"JARVIS_CAN_SPAWN_HEAVY='{gate_raw}' invalid (expected true/false)"
        )

    if gate_raw == "true" and mode in _HEAVY_BLOCKED_MODES:
        divergences.append(
            f"gate=true but effective_mode={mode} blocks heavy spawn"
        )

    if gate_raw == "true" and backend_minimal == "true":
        divergences.append(
            "gate=true but JARVIS_BACKEND_MINIMAL=true"
        )

    return ConsistencyResult(
        concept=concept,
        consistent=len(divergences) == 0,
        authoritative_value=gate_raw,
        divergences=divergences,
    )


# Map concept → validator function
_VALIDATORS: Dict[str, Callable[..., ConsistencyResult]] = {
    "gcp_vm_readiness": _validate_gcp_vm_readiness,
    "prime_routing_mode": _validate_prime_routing_mode,
    "startup_memory_mode": _validate_startup_memory_mode,
    "heavy_spawn_admission": _validate_heavy_spawn_admission,
}


# ===========================================================================
# Public Validation API
# ===========================================================================

def validate_consistency(
    concept: Optional[str] = None,
    *,
    supervisor: Optional[Any] = None,
    prime_router: Optional[Any] = None,
) -> List[ConsistencyResult]:
    """
    Check state consistency for one or all declared concepts.

    Args:
        concept: If provided, check only this concept. If None, check all.
        supervisor: The UnifiedSupervisor instance (needed for GCP VM readiness).
        prime_router: The PrimeRouter instance (needed for routing mode).

    Returns:
        List of ConsistencyResult, one per checked concept.
    """
    results: List[ConsistencyResult] = []
    kwargs = {"supervisor": supervisor, "prime_router": prime_router}

    if concept is not None:
        validator = _VALIDATORS.get(concept)
        if validator is not None:
            results.append(validator(**kwargs))
        return results

    for name, validator in _VALIDATORS.items():
        try:
            results.append(validator(**kwargs))
        except Exception as exc:
            results.append(ConsistencyResult(
                concept=name,
                consistent=True,
                authoritative_value=None,
                divergences=[f"validator error: {exc}"],
            ))

    return results


def validate_consistency_at_boot(
    supervisor: Optional[Any] = None,
) -> List[str]:
    """
    Boot-time convenience wrapper. Returns list of warning strings.

    Called from _startup_impl() after Phase 1. Safe to call with missing
    objects (skips concepts that need them). Returns empty list if all
    concepts are consistent.
    """
    warnings: List[str] = []
    try:
        results = validate_consistency(supervisor=supervisor)
        for r in results:
            if not r.consistent:
                for d in r.divergences:
                    warnings.append(f"[{r.concept}] {d}")
    except Exception as exc:
        warnings.append(f"[state_authority] validation error: {exc}")
    return warnings
