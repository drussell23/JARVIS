"""
Non-functional invariants — timeout ownership, cancellation, idempotency, reason codes.
"""
from typing import Dict, List

TIMEOUT_OWNERSHIP: Dict[str, str] = {
    "vision_inference": "ModelRouter",
    "health_probe": "PrimeRouter",
    "startup_phase": "ProgressAwareStartupController",
    "cross_repo_handshake": "ContractGate",
    "model_load": "ModelLifecycleManager",
}

CANCELLATION_POLICY: Dict[str, str] = {
    "inference_in_flight": "propagate_to_client",
    "health_probe": "abandon_silently",
    "startup_phase": "shield_then_timeout",
    "model_load": "propagate_to_client",
}

IDEMPOTENCY_SCOPE: Dict[str, str] = {
    "routing_decision": "request_id",
    "contract_check": "boot_session_id",
    "capability_refresh": "manifest_hash",
}

ROUTING_REASON_CODES: List[str] = [
    "primary_available",
    "primary_timeout",
    "capability_mismatch",
    "circuit_open",
    "provider_unavailable",
    "manifest_stale",
    "fallback_selected",
    "health_check_failed",
]

CONTRACT_REASON_CODES: List[str] = [
    "compatible",
    "version_incompatible",
    "schema_mismatch",
    "manifest_stale",
    "handshake_timeout",
    "service_unreachable",
    "degraded_mode",
]
