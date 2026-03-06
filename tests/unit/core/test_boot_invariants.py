"""Tests for BootInvariantChecker — runtime invariant enforcement with causal tracing.

Disease 10 — Startup Sequencing, Task 4.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.core.boot_invariants import (
    BootInvariantChecker,
    CausalTrace,
    InvariantResult,
    InvariantSeverity,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_state(**overrides: Any) -> Dict[str, Any]:
    """Build a default safe state dict, with optional overrides."""
    defaults: Dict[str, Any] = {
        # GCP-related
        "gcp_offload_active": False,
        "gcp_node_ip": None,
        "gcp_node_reachable": False,
        "gcp_handshake_complete": False,
        # Routing
        "routing_target": "local",
        "local_model_loaded": True,
        "cloud_fallback_enabled": False,
        # Boot
        "boot_phase": "core_ready",
    }
    defaults.update(overrides)
    return defaults


def _find(results: list[InvariantResult], invariant_id: str) -> InvariantResult:
    """Find a result by invariant_id, raising if not found."""
    for r in results:
        if r.invariant_id == invariant_id:
            return r
    raise AssertionError(f"Invariant {invariant_id!r} not found in results")


# ---------------------------------------------------------------------------
# TestInvariantNoRoutingWithoutHandshake (INV-1)
# ---------------------------------------------------------------------------


class TestInvariantNoRoutingWithoutHandshake:
    """INV-1: No routing to GCP without completed handshake."""

    def test_routing_gcp_with_handshake_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target="gcp", gcp_handshake_complete=True)
        results = checker.check_all(state)
        inv1 = _find(results, "INV-1")
        assert inv1.passed is True
        assert inv1.trace is None

    def test_routing_gcp_without_handshake_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target="gcp", gcp_handshake_complete=False)
        results = checker.check_all(state)
        inv1 = _find(results, "INV-1")
        assert inv1.passed is False
        assert inv1.severity is InvariantSeverity.CRITICAL
        assert inv1.trace is not None

    def test_routing_local_skips_handshake_check(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target="local", gcp_handshake_complete=False)
        results = checker.check_all(state)
        inv1 = _find(results, "INV-1")
        assert inv1.passed is True
        assert inv1.trace is None


# ---------------------------------------------------------------------------
# TestInvariantNoOffloadWithoutReachable (INV-2)
# ---------------------------------------------------------------------------


class TestInvariantNoOffloadWithoutReachable:
    """INV-2: No offload_active without reachable node."""

    def test_offload_with_reachable_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            gcp_offload_active=True,
            gcp_node_ip="10.0.0.1",
            gcp_node_reachable=True,
        )
        results = checker.check_all(state)
        inv2 = _find(results, "INV-2")
        assert inv2.passed is True
        assert inv2.trace is None

    def test_offload_without_ip_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            gcp_offload_active=True,
            gcp_node_ip=None,
            gcp_node_reachable=True,
        )
        results = checker.check_all(state)
        inv2 = _find(results, "INV-2")
        assert inv2.passed is False
        assert inv2.trace is not None

    def test_offload_with_unreachable_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            gcp_offload_active=True,
            gcp_node_ip="10.0.0.1",
            gcp_node_reachable=False,
        )
        results = checker.check_all(state)
        inv2 = _find(results, "INV-2")
        assert inv2.passed is False
        assert inv2.trace is not None


# ---------------------------------------------------------------------------
# TestInvariantNoDualAuthority (INV-3)
# ---------------------------------------------------------------------------


class TestInvariantNoDualAuthority:
    """INV-3: No dual authority — routing_target must be a known value."""

    def test_single_target_passes(self) -> None:
        checker = BootInvariantChecker()
        for target in ("local", "gcp", "cloud"):
            state = _make_state(routing_target=target, gcp_handshake_complete=True)
            results = checker.check_all(state)
            inv3 = _find(results, "INV-3")
            assert inv3.passed is True, f"Failed for target={target!r}"

    def test_no_target_during_boot_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target=None)
        results = checker.check_all(state)
        inv3 = _find(results, "INV-3")
        assert inv3.passed is True


# ---------------------------------------------------------------------------
# TestInvariantNoDeadEndFallback (INV-4)
# ---------------------------------------------------------------------------


class TestInvariantNoDeadEndFallback:
    """INV-4: No dead-end fallback — at least one inference path must exist."""

    def test_no_local_gcp_with_cloud_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            local_model_loaded=False,
            gcp_handshake_complete=False,
            cloud_fallback_enabled=True,
        )
        results = checker.check_all(state)
        inv4 = _find(results, "INV-4")
        assert inv4.passed is True
        assert inv4.trace is None

    def test_no_local_gcp_cloud_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            local_model_loaded=False,
            gcp_handshake_complete=False,
            cloud_fallback_enabled=False,
        )
        results = checker.check_all(state)
        inv4 = _find(results, "INV-4")
        assert inv4.passed is False
        assert inv4.severity is InvariantSeverity.CRITICAL
        assert inv4.trace is not None


# ---------------------------------------------------------------------------
# TestCausalTrace
# ---------------------------------------------------------------------------


class TestCausalTrace:
    """Verify causal trace presence on violations and absence on passes."""

    def test_violation_produces_trace(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target="gcp", gcp_handshake_complete=False)
        results = checker.check_all(state)
        inv1 = _find(results, "INV-1")
        assert inv1.passed is False
        assert inv1.trace is not None
        assert isinstance(inv1.trace, CausalTrace)
        assert inv1.trace.trigger != ""
        assert inv1.trace.decision != ""
        assert inv1.trace.timestamp > 0

    def test_passing_invariant_has_no_trace(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state()  # default is safe
        results = checker.check_all(state)
        for result in results:
            if result.passed:
                assert result.trace is None, (
                    f"Passing invariant {result.invariant_id} should have no trace"
                )
