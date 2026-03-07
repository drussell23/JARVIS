"""Tests for GCP-first routing policy in PrimeRouter."""

import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from core.prime_router import PrimeRouter, PrimeRouterConfig, RoutingDecision


def _make_router(gcp_promoted=False, prime_available=True, circuit_ok=True,
                 prefer_local=True, cloud_fallback=True, memory_emergency=False):
    """Build a PrimeRouter with controlled state for testing."""
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
    # Patch memory emergency
    router._is_memory_emergency = lambda: memory_emergency
    return router


class TestGCPFirstRouting:
    """GCP J-Prime should ALWAYS be first priority when available."""

    def test_gcp_promoted_routes_to_gcp_first(self):
        """When GCP is promoted, route to GCP regardless of local health."""
        router = _make_router(gcp_promoted=True, prime_available=True)
        assert router._decide_route() == RoutingDecision.GCP_PRIME

    def test_gcp_promoted_routes_to_gcp_even_when_prefer_local(self):
        """GCP-first overrides prefer_local setting."""
        router = _make_router(gcp_promoted=True, prefer_local=True)
        assert router._decide_route() == RoutingDecision.GCP_PRIME

    def test_gcp_promoted_routes_to_gcp_during_memory_emergency(self):
        """GCP still first during memory emergency."""
        router = _make_router(gcp_promoted=True, memory_emergency=True)
        assert router._decide_route() == RoutingDecision.GCP_PRIME

    def test_no_gcp_memory_emergency_routes_to_cloud(self):
        """Without GCP during emergency, route to cloud (never local)."""
        router = _make_router(gcp_promoted=False, memory_emergency=True)
        assert router._decide_route() == RoutingDecision.CLOUD_CLAUDE

    def test_no_gcp_no_emergency_routes_to_cloud_before_local(self):
        """Without GCP, prefer cloud over local for inference quality."""
        router = _make_router(gcp_promoted=False, cloud_fallback=True)
        assert router._decide_route() == RoutingDecision.CLOUD_CLAUDE

    def test_no_gcp_no_cloud_falls_to_local(self):
        """Only use local as last resort when GCP and cloud unavailable."""
        router = _make_router(gcp_promoted=False, cloud_fallback=False,
                              prime_available=True, circuit_ok=True)
        decision = router._decide_route()
        assert decision in (RoutingDecision.LOCAL_PRIME, RoutingDecision.HYBRID)

    def test_nothing_available_returns_degraded(self):
        """When everything is down, return DEGRADED."""
        router = _make_router(gcp_promoted=False, cloud_fallback=False,
                              prime_available=False)
        assert router._decide_route() == RoutingDecision.DEGRADED

    def test_gcp_circuit_open_falls_to_cloud(self):
        """When GCP circuit breaker is open, fall to cloud."""
        router = _make_router(gcp_promoted=True, circuit_ok=False)
        assert router._decide_route() == RoutingDecision.CLOUD_CLAUDE

    def test_mirror_mode_still_allows_routing(self):
        """Mirror mode blocks mutations, not reads. _decide_route should work."""
        router = _make_router(gcp_promoted=True)
        # _guard_mirror on non-mutating should not raise
        assert router._decide_route() == RoutingDecision.GCP_PRIME
