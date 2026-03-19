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
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

CONTRACT_VERSION = "1.0.0"


# =========================================================================
# SEVERITY & VIOLATION ENUMS (Disease 4: enforceable contracts)
# =========================================================================

class ContractSeverity(str, Enum):
    """How severely a contract violation should be treated.

    Ordered from most severe to least severe:
      PRECHECK_BLOCKER  - Fail before any component starts
      BOOT_BLOCKER      - Fail during boot sequence
      BLOCK_BEFORE_READY - Allow boot but block readiness advertisement
      DEGRADED_ALLOWED  - Allow startup in degraded mode with warnings
      ADVISORY          - Log only, never block (legacy behaviour)
    """
    PRECHECK_BLOCKER = "precheck_blocker"
    BOOT_BLOCKER = "boot_blocker"
    BLOCK_BEFORE_READY = "block_before_ready"
    DEGRADED_ALLOWED = "degraded_allowed"
    ADVISORY = "advisory"


class ViolationReasonCode(str, Enum):
    """Machine-readable reason why a contract was violated."""
    MALFORMED_URL = "malformed_url"
    PORT_CONFLICT = "port_conflict"
    PORT_OUT_OF_RANGE = "port_out_of_range"
    MISSING_SECRET = "missing_secret"
    CAPABILITY_MISSING = "capability_missing"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    VERSION_INCOMPATIBLE = "version_incompatible"
    HASH_DRIFT_DETECTED = "hash_drift_detected"
    HANDSHAKE_FAILED = "handshake_failed"
    HEALTH_UNREACHABLE = "health_unreachable"
    ALIAS_CONFLICT = "alias_conflict"
    PATTERN_MISMATCH = "pattern_mismatch"
    DEFAULT_FALLBACK_USED = "default_fallback_used"


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
    severity: ContractSeverity = ContractSeverity.ADVISORY


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
        severity=ContractSeverity.PRECHECK_BLOCKER,
    ),
    EnvContract(
        "JARVIS_FRONTEND_PORT", "Frontend dev server port",
        value_type="int",
        pattern=r"^\d{1,5}$",
        aliases=("FRONTEND_PORT",),
        default="3000",
        severity=ContractSeverity.PRECHECK_BLOCKER,
    ),
    EnvContract(
        "JARVIS_LOADING_SERVER_PORT", "Loading server port",
        value_type="int",
        pattern=r"^\d{1,5}$",
        aliases=("LOADING_SERVER_PORT", "JARVIS_LOADING_PORT"),
        default="3001",
        severity=ContractSeverity.PRECHECK_BLOCKER,
    ),

    # --- GCP / Cloud ---
    EnvContract(
        "JARVIS_PRIME_URL", "URL to J-Prime inference endpoint",
        value_type="url",
        pattern=r"^https?://[\w.\-]+:\d{1,5}(/.*)?$",
        severity=ContractSeverity.PRECHECK_BLOCKER,
    ),
    EnvContract(
        "GCP_PRIME_ENDPOINT", "GCP Prime endpoint (alias for JARVIS_PRIME_URL)",
        value_type="url",
        pattern=r"^https?://[\w.\-]+:\d{1,5}(/.*)?$",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),
    EnvContract(
        "JARVIS_INVINCIBLE_NODE_IP", "IP of the GCP Invincible Node",
        value_type="str",
        pattern=r"^[\d.]+$",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),
    EnvContract(
        "JARVIS_INVINCIBLE_NODE_PORT", "Port of the Invincible Node",
        value_type="int",
        pattern=r"^\d{1,5}$",
        default="8000",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),
    EnvContract(
        "JARVIS_GCP_OFFLOAD_ACTIVE", "Whether GCP offload is active",
        value_type="bool",
        pattern=r"^(true|false|1|0)$",
        default="false",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),
    EnvContract(
        "JARVIS_INVINCIBLE_NODE_BOOTING", "Whether Invincible Node is booting",
        value_type="bool",
        pattern=r"^(true|false|1|0)$",
        default="false",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),

    # --- Hollow Client ---
    EnvContract(
        "JARVIS_HOLLOW_CLIENT_ACTIVE", "Whether Hollow Client mode is active",
        value_type="bool",
        pattern=r"^(true|false|1|0)$",
        aliases=("JARVIS_HOLLOW_CLIENT", "JARVIS_HOLLOW_CLIENT_MODE"),
        default="false",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),

    # --- Backend ---
    EnvContract(
        "JARVIS_BACKEND_MINIMAL", "Whether backend is in minimal mode",
        value_type="bool",
        pattern=r"^(true|false)$",
        default="false",
        severity=ContractSeverity.DEGRADED_ALLOWED,
    ),

    # --- Memory ---
    EnvContract(
        "JARVIS_MEASURED_AVAILABLE_GB", "Measured available memory in GB",
        value_type="float",
        pattern=r"^\d+\.\d+$",
    ),
    EnvContract(
        "JARVIS_MEASURED_MEMORY_SOURCE", "Source used for startup memory measurements",
        value_type="str",
        pattern=r"^[a-z0-9_\-]+$",
    ),
    EnvContract(
        "JARVIS_MEASURED_MEMORY_TIER", "Memory tier at last startup/runtime admission check",
        value_type="str",
        pattern=r"^(abundant|optimal|elevated|constrained|critical|emergency|unknown)$",
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
# VIOLATION RECORDS (Disease 4: enforceable contracts)
# =========================================================================

@dataclass(frozen=True)
class ContractViolationRecord:
    """Immutable record of a single contract violation.

    Captures everything needed for enforcement decisions and forensic
    investigation: what contract failed, how bad it is, why it failed,
    where the value came from, and when the check ran.
    """
    contract_name: str
    base_severity: ContractSeverity
    effective_severity: ContractSeverity  # may differ from base via overrides
    reason_code: ViolationReasonCode
    violation: str  # human-readable description
    value_origin: str  # "explicit", "alias", "default", "missing"
    checked_at_monotonic: float
    checked_at_utc: str
    phase: str  # "precheck", "boot", "readiness", "runtime"


class StartupContractViolation(Exception):
    """Raised when one or more contract violations exceed the allowed severity.

    Carries structured violation records so callers can inspect, log, or
    report exactly which contracts failed and why.
    """

    def __init__(self, violations: List[ContractViolationRecord]) -> None:
        self.violations = violations
        lines = []
        for v in violations:
            lines.append(
                f"  [{v.effective_severity.value}] {v.contract_name}: "
                f"{v.violation} (reason={v.reason_code.value}, "
                f"origin={v.value_origin}, phase={v.phase})"
            )
        message = (
            f"{len(violations)} contract violation(s) prevent startup:\n"
            + "\n".join(lines)
        )
        super().__init__(message)


# =========================================================================
# CONTRACT STATE AUTHORITY (Disease 4: enforceable contracts)
# =========================================================================

class ContractStateAuthority:
    """Central authority for all contract violation state.

    Accumulates violations with dedup semantics: same (contract_name, reason_code)
    updates counter/timestamp rather than appending a new entry.
    Thread-safe via lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._violations: Dict[tuple, ContractViolationRecord] = {}
        self._counts: Dict[tuple, int] = {}

    def record(self, violation: ContractViolationRecord) -> None:
        key = (violation.contract_name, violation.reason_code)
        with self._lock:
            self._violations[key] = violation
            self._counts[key] = self._counts.get(key, 0) + 1

    def get_violations(self, *, severity_filter=None, phase_filter=None):
        with self._lock:
            records = list(self._violations.values())
        if severity_filter is not None:
            records = [r for r in records if r.effective_severity == severity_filter]
        if phase_filter is not None:
            records = [r for r in records if r.phase == phase_filter]
        return records

    def has_blockers(self) -> bool:
        with self._lock:
            return any(
                v.effective_severity in (ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER)
                for v in self._violations.values()
            )

    def blocking_reasons(self):
        with self._lock:
            return [
                v.reason_code.value
                for v in self._violations.values()
                if v.effective_severity in (ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER)
            ]

    def health_summary(self, *, max_detail: int = 5):
        with self._lock:
            all_v = list(self._violations.values())
        blockers = [v for v in all_v if v.effective_severity in (
            ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER
        )]
        return {
            "total_violations": len(all_v),
            "blocker_count": len(blockers),
            "top_blockers": [
                {"contract": v.contract_name, "reason": v.reason_code.value}
                for v in blockers[:max_detail]
            ],
        }

    def full_report(self):
        with self._lock:
            all_v = list(self._violations.values())
            counts = dict(self._counts)
        return {
            "violations": [
                {
                    "contract_name": v.contract_name,
                    "severity": v.effective_severity.value,
                    "reason_code": v.reason_code.value,
                    "violation": v.violation,
                    "value_origin": v.value_origin,
                    "phase": v.phase,
                    "occurrence_count": counts.get((v.contract_name, v.reason_code), 1),
                }
                for v in all_v
            ],
        }


# =========================================================================
# CONTRACT SNAPSHOTS (drift detection)
# =========================================================================

@dataclass(frozen=True)
class ContractSnapshot:
    """Point-in-time snapshot of a contract check for drift detection.

    Stored at initial check and compared at CONTRACT_GATE to detect
    if contract state changed during startup.
    """
    target: str
    schema_hash: str
    capability_hash: str
    session_id: str
    checked_at_monotonic: float


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

def validate_contracts_at_boot() -> List[ContractViolationRecord]:
    """Validate all cross-repo contracts at startup.

    Returns structured violation records with severity, reason codes, and origin tracing.
    Callers decide enforcement based on severity.
    """
    import time as _time
    from datetime import datetime, timezone

    violations: List[ContractViolationRecord] = []
    now_mono = _time.monotonic()
    now_utc = datetime.now(timezone.utc).isoformat()

    for contract in ENV_CONTRACTS:
        resolution = get_canonical_env(contract.canonical_name)
        if resolution is None:
            continue  # Not set, no default — nothing to validate

        val = resolution.value

        # Pattern validation
        port_range_emitted = False
        if contract.pattern and not re.match(contract.pattern, val):
            reason = ViolationReasonCode.PATTERN_MISMATCH
            if contract.value_type == "int" and val.isdigit():
                port = int(val)
                if port < 1 or port > 65535:
                    reason = ViolationReasonCode.PORT_OUT_OF_RANGE
                    port_range_emitted = True
            elif contract.value_type == "url":
                reason = ViolationReasonCode.MALFORMED_URL

            violations.append(ContractViolationRecord(
                contract_name=contract.canonical_name,
                base_severity=contract.severity,
                effective_severity=contract.severity,
                reason_code=reason,
                violation=(
                    f"{contract.canonical_name}={val!r} does not match "
                    f"expected pattern {contract.pattern} ({contract.description})"
                ),
                value_origin=resolution.origin,
                checked_at_monotonic=now_mono,
                checked_at_utc=now_utc,
                phase="precheck",
            ))

        # Semantic port range check (pattern may pass for values like "99999")
        if (not port_range_emitted
                and contract.value_type == "int"
                and "PORT" in contract.canonical_name
                and val.isdigit()):
            port = int(val)
            if port < 1 or port > 65535:
                violations.append(ContractViolationRecord(
                    contract_name=contract.canonical_name,
                    base_severity=contract.severity,
                    effective_severity=contract.severity,
                    reason_code=ViolationReasonCode.PORT_OUT_OF_RANGE,
                    violation=(
                        f"{contract.canonical_name}={val!r} port {port} is outside "
                        f"valid range 1-65535 ({contract.description})"
                    ),
                    value_origin=resolution.origin,
                    checked_at_monotonic=now_mono,
                    checked_at_utc=now_utc,
                    phase="precheck",
                ))

        # Alias conflict detection
        for alias in contract.aliases:
            alias_val = os.environ.get(alias)
            if alias_val is not None and alias_val != val:
                violations.append(ContractViolationRecord(
                    contract_name=contract.canonical_name,
                    base_severity=contract.severity,
                    effective_severity=contract.severity,
                    reason_code=ViolationReasonCode.ALIAS_CONFLICT,
                    violation=(
                        f"Alias conflict: {contract.canonical_name}={val!r} "
                        f"but {alias}={alias_val!r} ({contract.description})"
                    ),
                    value_origin=resolution.origin,
                    checked_at_monotonic=now_mono,
                    checked_at_utc=now_utc,
                    phase="precheck",
                ))

    # Port collision detection (PRECHECK_BLOCKER)
    port_contracts = [c for c in ENV_CONTRACTS if c.value_type == "int" and "PORT" in c.canonical_name]
    port_values: Dict[str, str] = {}  # port_value -> contract_name
    for pc in port_contracts:
        res = get_canonical_env(pc.canonical_name)
        if res is not None and res.value.isdigit():
            if res.value in port_values:
                violations.append(ContractViolationRecord(
                    contract_name=pc.canonical_name,
                    base_severity=ContractSeverity.PRECHECK_BLOCKER,
                    effective_severity=ContractSeverity.PRECHECK_BLOCKER,
                    reason_code=ViolationReasonCode.PORT_CONFLICT,
                    violation=(
                        f"Port {res.value} claimed by both {port_values[res.value]} "
                        f"and {pc.canonical_name}"
                    ),
                    value_origin=res.origin,
                    checked_at_monotonic=now_mono,
                    checked_at_utc=now_utc,
                    phase="precheck",
                ))
            else:
                port_values[res.value] = pc.canonical_name

    return violations


def validate_and_enforce(logger: Optional[logging.Logger] = None) -> List[ContractViolationRecord]:
    """Validate all cross-repo contracts and enforce blocking severities.

    PRECHECK_BLOCKER violations raise LifecycleFatalError immediately.
    BOOT_BLOCKER violations raise LifecycleFatalError immediately.
    Lower severities are logged and returned for caller awareness.

    Returns the full violation list (non-fatal violations only) so callers
    can decide whether to advertise degraded mode.

    Raises:
        LifecycleFatalError: on any PRECHECK_BLOCKER or BOOT_BLOCKER violation.
    """
    from backend.core.lifecycle_exceptions import LifecycleFatalError, LifecycleErrorCode, LifecyclePhase

    log = logger or logging.getLogger(__name__)
    violations = validate_contracts_at_boot()

    blocking = [
        v for v in violations
        if v.effective_severity in (ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER)
    ]
    non_blocking = [
        v for v in violations
        if v.effective_severity not in (ContractSeverity.PRECHECK_BLOCKER, ContractSeverity.BOOT_BLOCKER)
    ]

    for v in non_blocking:
        log.warning(
            "[Contracts] %s violation (%s): %s [origin: %s]",
            v.effective_severity.value, v.reason_code.value, v.violation, v.value_origin,
        )

    if blocking:
        # Log every blocker before raising so the operator sees all problems at once.
        for v in blocking:
            log.critical(
                "[Contracts] BLOCKING violation (%s/%s): %s [origin: %s]",
                v.effective_severity.value, v.reason_code.value, v.violation, v.value_origin,
            )
        first = blocking[0]
        raise LifecycleFatalError(
            f"Boot blocked by {len(blocking)} contract violation(s). "
            f"First: {first.violation}",
            error_code=LifecycleErrorCode.CONTRACT_INCOMPATIBLE,
            state_at_raise="initializing",
            phase=LifecyclePhase.PRECHECK,
            epoch=0,
        )

    return non_blocking


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
            if expected_type == "bool" and val is not None and not isinstance(val, bool):
                # v293.0: None (null) means "not yet ready" — not a schema violation.
                # GCP VMs return ready_for_inference: null while the model is still
                # loading. Treating null as a violation causes crash loops because
                # the contract gate fires before the VM finishes initializing.
                # Null is normalized to False by the caller; only non-null non-bool
                # values are true type violations.
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


@dataclass(frozen=True)
class EnvResolution:
    """Result of resolving a contracted env var with origin tracing."""
    value: str
    origin: str           # "explicit" | "default" | "alias:{alias_name}" | "derived"
    canonical_name: str


def get_canonical_env(contract_name: str) -> Optional[EnvResolution]:
    """Get the value of a contracted env var with origin tracing.

    Returns EnvResolution with value + origin, or None if unset with no default.
    Callers that only need the value use get_canonical_env(...).value.
    """
    contract = _CONTRACT_MAP.get(contract_name)
    if contract is None:
        val = os.environ.get(contract_name)
        if val is None:
            return None
        return EnvResolution(value=val, origin="explicit", canonical_name=contract_name)

    # Try canonical first
    val = os.environ.get(contract.canonical_name)
    if val is not None:
        return EnvResolution(value=val, origin="explicit", canonical_name=contract.canonical_name)

    # Try aliases in order
    for alias in contract.aliases:
        val = os.environ.get(alias)
        if val is not None:
            return EnvResolution(value=val, origin=f"alias:{alias}", canonical_name=contract.canonical_name)

    # Default fallback
    if contract.default is not None:
        return EnvResolution(value=contract.default, origin="default", canonical_name=contract.canonical_name)

    return None
