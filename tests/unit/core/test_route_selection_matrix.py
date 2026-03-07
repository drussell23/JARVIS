"""Exhaustive route selection matrix for PrimeRouter._decide_route.

Tests every combination of:
  - GCP promoted (yes/no)
  - Circuit breaker (open/closed)
  - Memory emergency (yes/no)
  - Cloud fallback enabled (yes/no)
  - Local prime available (yes/no)
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from core.prime_router import PrimeRouter, PrimeRouterConfig, RoutingDecision


def _make_router(gcp_promoted=False, prime_available=True, circuit_ok=True,
                 prefer_local=True, cloud_fallback=True, memory_emergency=False):
    config = PrimeRouterConfig()
    config.prefer_local = prefer_local
    config.enable_cloud_fallback = cloud_fallback
    router = PrimeRouter.__new__(PrimeRouter)
    router._config = config
    router._metrics = MagicMock()
    router._prime_client = MagicMock() if prime_available else None
    if router._prime_client:
        router._prime_client.is_available = True
    router._cloud_client = None
    router._graceful_degradation = None
    router._lock = MagicMock()
    router._initialized = True
    router._gcp_promoted = gcp_promoted
    router._gcp_host = "34.45.154.209" if gcp_promoted else None
    router._gcp_port = 8080 if gcp_promoted else None
    router._local_circuit = MagicMock()
    router._local_circuit.can_execute.return_value = circuit_ok
    router._last_transition_time = 0.0
    router._transition_cooldown_s = 30.0
    router._transition_in_flight = False
    router._mirror_mode = False
    router._mirror_decisions_issued = 0
    router._cloud_run_patterns = (".run.app", ".a.run.app")
    router._is_memory_emergency = lambda: memory_emergency
    return router


# Matrix: (gcp, circuit, emergency, cloud, local) -> expected
ROUTE_MATRIX = [
    # GCP available and healthy -- always GCP
    (True, True, False, True, True, RoutingDecision.GCP_PRIME),
    (True, True, False, True, False, RoutingDecision.GCP_PRIME),
    (True, True, False, False, True, RoutingDecision.GCP_PRIME),
    (True, True, True, True, True, RoutingDecision.GCP_PRIME),

    # GCP promoted but circuit open -- fall to cloud
    (True, False, False, True, True, RoutingDecision.CLOUD_CLAUDE),
    (True, False, True, True, True, RoutingDecision.CLOUD_CLAUDE),

    # No GCP, cloud available -- cloud
    (False, True, False, True, True, RoutingDecision.CLOUD_CLAUDE),
    (False, True, True, True, True, RoutingDecision.CLOUD_CLAUDE),

    # No GCP, no cloud, local available, no emergency -- local
    (False, True, False, False, True, {RoutingDecision.LOCAL_PRIME, RoutingDecision.HYBRID}),

    # No GCP, no cloud, emergency -- degraded (local blocked)
    (False, True, True, False, True, RoutingDecision.DEGRADED),

    # Nothing available -- degraded
    (False, False, False, False, False, RoutingDecision.DEGRADED),
    (False, True, True, False, False, RoutingDecision.DEGRADED),
]


@pytest.mark.parametrize(
    "gcp,circuit,emergency,cloud,local,expected",
    ROUTE_MATRIX,
    ids=[
        "gcp_healthy_cloud_local",
        "gcp_healthy_cloud_nolocal",
        "gcp_healthy_nocloud_local",
        "gcp_healthy_emergency",
        "gcp_circuit_open_cloud",
        "gcp_circuit_open_emergency",
        "no_gcp_cloud",
        "no_gcp_cloud_emergency",
        "no_gcp_no_cloud_local",
        "no_gcp_no_cloud_emergency",
        "nothing_available",
        "nothing_emergency",
    ],
)
def test_route_matrix(gcp, circuit, emergency, cloud, local, expected):
    router = _make_router(
        gcp_promoted=gcp,
        circuit_ok=circuit,
        memory_emergency=emergency,
        cloud_fallback=cloud,
        prime_available=local,
    )
    result = router._decide_route()

    if isinstance(expected, set):
        assert result in expected, f"Expected one of {expected}, got {result}"
    else:
        assert result == expected, f"Expected {expected}, got {result}"
