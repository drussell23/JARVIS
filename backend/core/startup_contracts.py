"""
JARVIS Cross-Repo Startup Contracts v1.0
==========================================
Defines versioned contracts for cross-repo integration surfaces:
  1. Environment variable contracts (name, type, pattern, aliases)
  2. Health endpoint response schemas (required fields, types)
  3. Boot-time validation (advisory warnings, never blocks startup)

Rationale:
    23+ env vars are set by one component and read by another with zero
    validation. 3 different env var names map to the same port (8010).
    Health endpoint shapes are assumed but never checked. This module
    makes contracts explicit and drift detectable.

v270.3: Created as part of Phase 6 hardening.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CONTRACT_VERSION = "1.0.0"


# =========================================================================
# ENVIRONMENT VARIABLE CONTRACTS
# =========================================================================

@dataclass(frozen=True)
class EnvContract:
    """Schema for a cross-repo environment variable."""
    canonical_name: str
    description: str
    value_type: str = "str"  # str, int, float, bool, url
    pattern: Optional[str] = None  # regex for validation
    aliases: tuple = ()  # legacy names that map to same concept
    default: Optional[str] = None
    version: str = "1.0.0"


# Registry of all cross-repo environment variable contracts.
# Each entry documents: who writes it, what format it must be, and
# what alternative names exist in legacy code.
ENV_CONTRACTS: List[EnvContract] = [
    # --- Identity / Mode ---
    EnvContract(
        "JARVIS_STARTUP_MEMORY_MODE", "Startup mode (monotonic degradation)",
        value_type="str",
        pattern=r"^(local_full|local_optimized|sequential|cloud_first|cloud_only|minimal)$",
        default="local_full",
    ),
    EnvContract(
        "JARVIS_STARTUP_DESIRED_MODE", "Operator-intended startup mode",
        value_type="str",
        pattern=r"^(local_full|local_optimized|sequential|cloud_first|cloud_only|minimal)$",
    ),
    EnvContract(
        "JARVIS_STARTUP_EFFECTIVE_MODE", "Runtime effective startup mode",
        value_type="str",
        pattern=r"^(local_full|local_optimized|sequential|cloud_first|cloud_only|minimal)$",
    ),
    EnvContract(
        "JARVIS_CAN_SPAWN_HEAVY", "Shared heavy-init admission gate",
        value_type="bool",
        pattern=r"^(true|false)$",
        default="true",
    ),
    EnvContract(
        "JARVIS_HEAVY_ADMISSION_REASON", "Reason for heavy-init gate decision",
        value_type="str",
    ),
    EnvContract(
        "JARVIS_HEAVY_ADMISSION_CONTEXT", "Lifecycle context that published admission decision",
        value_type="str",
    ),
    EnvContract(
        "JARVIS_HEAVY_ADMISSION_AVAILABLE_GB", "Measured available memory at admission decision",
        value_type="float",
        pattern=r"^\d+\.\d+$",
    ),
    EnvContract(
        "JARVIS_STARTUP_COMPLETE", "Whether startup has completed",
        value_type="bool",
        pattern=r"^(true|false)$",
        default="false",
    ),

    # --- Ports ---
    EnvContract(
        "JARVIS_BACKEND_PORT", "Backend API port",
        value_type="int",
        pattern=r"^\d{1,5}$",
        aliases=("BACKEND_PORT", "JARVIS_API_PORT", "JARVIS_PORT"),
        default="8010",
    ),
    EnvContract(
        "JARVIS_FRONTEND_PORT", "Frontend dev server port",
        value_type="int",
        pattern=r"^\d{1,5}$",
        aliases=("FRONTEND_PORT",),
        default="3000",
    ),
    EnvContract(
        "JARVIS_LOADING_SERVER_PORT", "Loading server port",
        value_type="int",
        pattern=r"^\d{1,5}$",
        aliases=("LOADING_SERVER_PORT", "JARVIS_LOADING_PORT"),
        default="3001",
    ),

    # --- GCP / Cloud ---
    EnvContract(
        "JARVIS_PRIME_URL", "URL to J-Prime inference endpoint",
        value_type="url",
        pattern=r"^https?://[\w.\-]+:\d{1,5}(/.*)?$",
    ),
    EnvContract(
        "GCP_PRIME_ENDPOINT", "GCP Prime endpoint (alias for JARVIS_PRIME_URL)",
        value_type="url",
        pattern=r"^https?://[\w.\-]+:\d{1,5}(/.*)?$",
    ),
    EnvContract(
        "JARVIS_INVINCIBLE_NODE_IP", "IP of the GCP Invincible Node",
        value_type="str",
        pattern=r"^[\d.]+$",
    ),
    EnvContract(
        "JARVIS_INVINCIBLE_NODE_PORT", "Port of the Invincible Node",
        value_type="int",
        pattern=r"^\d{1,5}$",
        default="8001",
    ),
    EnvContract(
        "JARVIS_GCP_OFFLOAD_ACTIVE", "Whether GCP offload is active",
        value_type="bool",
        pattern=r"^(true|false|1|0)$",
        default="false",
    ),
    EnvContract(
        "JARVIS_INVINCIBLE_NODE_BOOTING", "Whether Invincible Node is booting",
        value_type="bool",
        pattern=r"^(true|false|1|0)$",
        default="false",
    ),

    # --- Hollow Client ---
    EnvContract(
        "JARVIS_HOLLOW_CLIENT_ACTIVE", "Whether Hollow Client mode is active",
        value_type="bool",
        pattern=r"^(true|false|1|0)$",
        aliases=("JARVIS_HOLLOW_CLIENT", "JARVIS_HOLLOW_CLIENT_MODE"),
        default="false",
    ),

    # --- Backend ---
    EnvContract(
        "JARVIS_BACKEND_MINIMAL", "Whether backend is in minimal mode",
        value_type="bool",
        pattern=r"^(true|false)$",
        default="false",
    ),

    # --- Memory ---
    EnvContract(
        "JARVIS_MEASURED_AVAILABLE_GB", "Measured available memory in GB",
        value_type="float",
        pattern=r"^\d+\.\d+$",
    ),
]

# Index by canonical name for fast lookup
_CONTRACT_MAP: Dict[str, EnvContract] = {c.canonical_name: c for c in ENV_CONTRACTS}

# Build alias → canonical mapping
_ALIAS_MAP: Dict[str, str] = {}
for _c in ENV_CONTRACTS:
    for _a in _c.aliases:
        _ALIAS_MAP[_a] = _c.canonical_name


# =========================================================================
# HEALTH ENDPOINT SCHEMAS
# =========================================================================

@dataclass(frozen=True)
class HealthEndpointSchema:
    """Expected shape of a health endpoint response."""
    path: str
    required_fields: Dict[str, str] = field(default_factory=dict)  # field_name -> type
    version: str = "1.0.0"


HEALTH_SCHEMAS: Dict[str, HealthEndpointSchema] = {
    "/health": HealthEndpointSchema(
        path="/health",
        required_fields={"status": "str"},
    ),
    "/health/ready": HealthEndpointSchema(
        path="/health/ready",
        required_fields={"ready": "bool"},
        version="2.0.0",
    ),
    "prime:/health": HealthEndpointSchema(
        path="prime:/health",
        required_fields={
            "ready_for_inference": "bool",
        },
    ),
}


# =========================================================================
# VALIDATION FUNCTIONS
# =========================================================================

def validate_contracts_at_boot() -> List[str]:
    """Validate all cross-repo contracts at startup.

    Returns a list of warning strings. NEVER raises exceptions or blocks
    startup — contract violations are advisory, not fatal.

    Checks:
      1. Env vars with unexpected format (type mismatch, pattern violation)
      2. Alias conflicts (two names for same concept set to different values)
    """
    warnings: List[str] = []

    for contract in ENV_CONTRACTS:
        val = os.environ.get(contract.canonical_name)
        if val is None:
            continue  # Not set is fine — many are optional

        # Pattern validation
        if contract.pattern and not re.match(contract.pattern, val):
            warnings.append(
                f"{contract.canonical_name}={val!r} does not match "
                f"expected pattern {contract.pattern} "
                f"({contract.description})"
            )

        # Alias conflict detection
        for alias in contract.aliases:
            alias_val = os.environ.get(alias)
            if alias_val is not None and alias_val != val:
                warnings.append(
                    f"Alias conflict: {contract.canonical_name}={val!r} "
                    f"but {alias}={alias_val!r} — these should match "
                    f"({contract.description})"
                )

    return warnings


def validate_health_response(
    endpoint: str,
    data: Dict[str, Any],
) -> List[str]:
    """Validate a health endpoint response against its schema.

    Args:
        endpoint: Schema key (e.g., "/health", "prime:/health")
        data: The parsed JSON response

    Returns:
        List of violation strings. Empty list means valid.
    """
    schema = HEALTH_SCHEMAS.get(endpoint)
    if schema is None:
        return []  # No schema defined — cannot validate

    violations: List[str] = []
    for field_name, expected_type in schema.required_fields.items():
        if field_name not in data:
            violations.append(
                f"{endpoint} missing required field '{field_name}' "
                f"(expected {expected_type})"
            )
        else:
            val = data[field_name]
            # Basic type checking
            type_ok = True
            if expected_type == "bool" and not isinstance(val, bool):
                type_ok = False
            elif expected_type == "str" and not isinstance(val, str):
                type_ok = False
            elif expected_type == "int" and not isinstance(val, int):
                type_ok = False
            elif expected_type == "float" and not isinstance(val, (int, float)):
                type_ok = False
            elif expected_type == "dict" and not isinstance(val, dict):
                type_ok = False

            if not type_ok:
                violations.append(
                    f"{endpoint} field '{field_name}' expected "
                    f"{expected_type} but got {type(val).__name__}"
                )

    return violations


def get_canonical_env(contract_name: str) -> Optional[str]:
    """Get the value of a contracted env var, checking aliases as fallback.

    This is the migration bridge — consumers can call this instead of
    os.getenv() directly to get consistent behavior regardless of which
    legacy env var name was set.

    Args:
        contract_name: The canonical env var name

    Returns:
        The value (from canonical or alias), or None if unset.
    """
    contract = _CONTRACT_MAP.get(contract_name)
    if contract is None:
        # Not a contracted var — fall through to raw getenv
        return os.environ.get(contract_name)

    # Try canonical first
    val = os.environ.get(contract.canonical_name)
    if val is not None:
        return val

    # Try aliases in order
    for alias in contract.aliases:
        val = os.environ.get(alias)
        if val is not None:
            return val

    return contract.default
