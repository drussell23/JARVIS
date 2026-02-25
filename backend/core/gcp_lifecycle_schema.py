# backend/core/gcp_lifecycle_schema.py
"""Canonical schema for GCP lifecycle state machine.

All journal rows, UDS payloads, state machine internals, and tests
use these exact enum values. Reject unknown strings at boundaries.

Design doc: docs/plans/2026-02-25-journal-backed-gcp-lifecycle-design.md
Section 1B: Canonical Schema.
"""
from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    TRIGGERING = "triggering"
    PROVISIONING = "provisioning"
    BOOTING = "booting"
    HANDSHAKING = "handshaking"
    ACTIVE = "active"
    COOLING_DOWN = "cooling_down"
    STOPPING = "stopping"
    LOST = "lost"
    FAILED = "failed"
    DEGRADED = "degraded"


class Event(str, Enum):
    # Pressure / trigger
    PRESSURE_TRIGGERED = "pressure_triggered"
    PRESSURE_COOLED = "pressure_cooled"
    RETRIGGER_DURING_COOLDOWN = "retrigger_during_cooldown"
    COOLDOWN_EXPIRED = "cooldown_expired"
    # Budget
    BUDGET_CHECK = "budget_check"
    BUDGET_APPROVED = "budget_approved"
    BUDGET_DENIED = "budget_denied"
    BUDGET_EXHAUSTED_RUNTIME = "budget_exhausted_runtime"
    BUDGET_RELEASED = "budget_released"
    # Provisioning / VM
    PROVISION_REQUESTED = "provision_requested"
    VM_CREATE_ACCEPTED = "vm_create_accepted"
    VM_CREATE_ALREADY_EXISTS = "vm_create_already_exists"
    VM_CREATE_FAILED = "vm_create_failed"
    VM_READY = "vm_ready"
    VM_STOP_REQUESTED = "vm_stop_requested"
    VM_STOPPED = "vm_stopped"
    VM_STOP_TIMEOUT = "vm_stop_timeout"
    SPOT_PREEMPTED = "spot_preempted"
    # Health / handshake
    HEALTH_PROBE_OK = "health_probe_ok"
    HEALTH_PROBE_DEGRADED = "health_probe_degraded"
    HEALTH_PROBE_TIMEOUT = "health_probe_timeout"
    HEALTH_UNREACHABLE_CONSECUTIVE = "health_unreachable_consecutive"
    HEALTH_DEGRADED_CONSECUTIVE = "health_degraded_consecutive"
    HANDSHAKE_STARTED = "handshake_started"
    HANDSHAKE_SUCCEEDED = "handshake_succeeded"
    HANDSHAKE_FAILED = "handshake_failed"
    BOOT_DEADLINE_EXCEEDED = "boot_deadline_exceeded"
    # Routing / reconcile / audit
    ROUTING_SWITCHED_TO_LOCAL = "routing_switched_to_local"
    ROUTING_SWITCHED_TO_CLOUD = "routing_switched_to_cloud"
    RECONCILE_OBSERVED_RUNNING = "reconcile_observed_running"
    RECONCILE_OBSERVED_STOPPED = "reconcile_observed_stopped"
    AUDIT_RECONCILE = "audit_reconcile"
    # Control-plane / operator
    LEASE_LOST = "lease_lost"
    SESSION_SHUTDOWN = "session_shutdown"
    MANUAL_FORCE_LOCAL = "manual_force_local"
    MANUAL_FORCE_CLOUD = "manual_force_cloud"
    FATAL_ERROR = "fatal_error"


class HealthCategory(str, Enum):
    HEALTHY = "healthy"
    CONTRACT_MISMATCH = "contract_mismatch"
    DEPENDENCY_DEGRADED = "dependency_degraded"
    SERVICE_DEGRADED = "service_degraded"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class DisconnectReason(str, Enum):
    TIMEOUT = "timeout"
    WRITE_ERROR = "write_error"
    EOF = "eof"
    PROTOCOL_ERROR = "protocol_error"
    LEASE_LOST = "lease_lost"
    SERVER_SHUTDOWN = "server_shutdown"
    CLIENT_SHUTDOWN = "client_shutdown"


def validate_state(value: str) -> State:
    try:
        return State(value)
    except ValueError:
        raise ValueError(f"Unknown state: {value!r}. Valid: {[s.value for s in State]}")


def validate_event(value: str) -> Event:
    try:
        return Event(value)
    except ValueError:
        raise ValueError(f"Unknown event: {value!r}. Valid: {[e.value for e in Event]}")


def validate_health_category(value: str) -> HealthCategory:
    try:
        return HealthCategory(value)
    except ValueError:
        raise ValueError(
            f"Unknown health category: {value!r}. "
            f"Valid: {[h.value for h in HealthCategory]}"
        )
