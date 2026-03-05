"""
Routing authority declarations — who owns which decision.
"""
from enum import Enum
from typing import Dict

from .contract_version import compute_policy_hash


class RoutingAuthority(Enum):
    """Which system owns which routing concern."""
    POLICY = "ModelRouter"
    HEALTH = "PrimeRouter"
    DATA = "ModelRegistry"


ROUTING_INVARIANTS: Dict[str, str] = {
    "vision_provider_selection": RoutingAuthority.POLICY.value,
    "chat_provider_selection": RoutingAuthority.POLICY.value,
    "endpoint_health_check": RoutingAuthority.HEALTH.value,
    "endpoint_failover": RoutingAuthority.HEALTH.value,
    "model_capability_data": RoutingAuthority.DATA.value,
    "model_lifecycle_state": RoutingAuthority.DATA.value,
    "circuit_breaker_state": RoutingAuthority.POLICY.value,
}


def get_routing_policy_hash() -> str:
    """Compute deterministic hash of routing invariants for drift detection."""
    return compute_policy_hash(ROUTING_INVARIANTS)
