"""Tests for startup_orchestrator — Disease 10 lifecycle coordinator.

Disease 10 Wiring, Task 7.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.startup_orchestrator import (
    StartupOrchestrator,
    OrchestratorState,
)
from backend.core.startup_config import load_startup_config
from backend.core.startup_phase_gate import GateStatus, StartupPhase
from backend.core.startup_routing_policy import BootRoutingDecision, FallbackReason
from backend.core.routing_authority_fsm import AuthorityState


class FakeProber:
    """Prober that always passes all steps."""
    async def probe_health(self, host, port, timeout):
        from backend.core.gcp_readiness_lease import HandshakeResult, HandshakeStep
        return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)

    async def probe_capabilities(self, host, port, timeout):
        from backend.core.gcp_readiness_lease import HandshakeResult, HandshakeStep
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)

    async def probe_warm_model(self, host, port, timeout):
        from backend.core.gcp_readiness_lease import HandshakeResult, HandshakeStep
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)


@pytest.fixture
def orchestrator() -> StartupOrchestrator:
    cfg = load_startup_config()
    return StartupOrchestrator(config=cfg, prober=FakeProber())


class TestOrchestratorLifecycle:

    def test_initial_state(self, orchestrator):
        assert orchestrator.authority_state == AuthorityState.BOOT_POLICY_ACTIVE
        assert orchestrator.current_phase is None

    async def test_resolve_prewarm_gcp(self, orchestrator):
        result = await orchestrator.resolve_phase("PREWARM_GCP", detail="gcp ready")
        assert result.status == GateStatus.PASSED

    async def test_skip_prewarm_gcp(self, orchestrator):
        result = await orchestrator.skip_phase("PREWARM_GCP", reason="no gcp")
        assert result.status == GateStatus.SKIPPED

    async def test_full_phase_chain(self, orchestrator):
        await orchestrator.resolve_phase("PREWARM_GCP")
        await orchestrator.resolve_phase("CORE_SERVICES")
        await orchestrator.resolve_phase("CORE_READY")
        await orchestrator.resolve_phase("DEFERRED_COMPONENTS")
        # All phases resolved
        snap = orchestrator.gate_snapshot()
        for phase_snap in snap.values():
            assert phase_snap.status in (GateStatus.PASSED, GateStatus.SKIPPED)

    async def test_phase_with_unmet_dependency_fails(self, orchestrator):
        result = await orchestrator.resolve_phase("CORE_SERVICES")
        assert result.status == GateStatus.FAILED


class TestGCPLeaseIntegration:

    async def test_acquire_gcp_lease(self, orchestrator):
        success = await orchestrator.acquire_gcp_lease("10.0.0.1", 8000)
        assert success is True
        assert orchestrator.lease_valid is True

    async def test_revoke_gcp_lease(self, orchestrator):
        await orchestrator.acquire_gcp_lease("10.0.0.1", 8000)
        orchestrator.revoke_gcp_lease("spot_preemption")
        assert orchestrator.lease_valid is False

    async def test_lease_signals_routing_policy(self, orchestrator):
        await orchestrator.acquire_gcp_lease("10.0.0.1", 8000)
        decision, reason = orchestrator.routing_decide()
        assert decision == BootRoutingDecision.GCP_PRIME


class TestAuthorityHandoff:

    async def test_handoff_after_core_ready(self, orchestrator):
        # Setup: resolve all gates up to CORE_READY
        await orchestrator.resolve_phase("PREWARM_GCP")
        await orchestrator.resolve_phase("CORE_SERVICES")
        await orchestrator.resolve_phase("CORE_READY")
        await orchestrator.acquire_gcp_lease("10.0.0.1", 8000)

        # Mock the hybrid router
        mock_hybrid = MagicMock()
        mock_hybrid.is_disease10_active = False
        orchestrator.set_hybrid_router(mock_hybrid)

        result = await orchestrator.attempt_handoff()
        assert result.success is True
        assert orchestrator.authority_state == AuthorityState.HYBRID_ACTIVE
        mock_hybrid.set_active.assert_called_once_with(True)

    async def test_handoff_fails_without_core_ready(self, orchestrator):
        await orchestrator.resolve_phase("PREWARM_GCP")
        result = await orchestrator.attempt_handoff()
        assert result.success is False
        assert orchestrator.authority_state == AuthorityState.BOOT_POLICY_ACTIVE


class TestRecovery:

    async def test_lease_loss_triggers_rollback(self, orchestrator):
        # Get to HYBRID_ACTIVE
        await orchestrator.resolve_phase("PREWARM_GCP")
        await orchestrator.resolve_phase("CORE_SERVICES")
        await orchestrator.resolve_phase("CORE_READY")
        await orchestrator.acquire_gcp_lease("10.0.0.1", 8000)
        mock_hybrid = MagicMock()
        mock_hybrid.is_disease10_active = False
        orchestrator.set_hybrid_router(mock_hybrid)
        await orchestrator.attempt_handoff()
        assert orchestrator.authority_state == AuthorityState.HYBRID_ACTIVE

        # Revoke lease — should trigger rollback
        await orchestrator.handle_lease_loss("spot_preemption")
        assert orchestrator.authority_state == AuthorityState.BOOT_POLICY_ACTIVE
        mock_hybrid.set_active.assert_called_with(False)


class TestInvariantChecks:

    async def test_invariants_checked_after_routing(self, orchestrator):
        await orchestrator.acquire_gcp_lease("10.0.0.1", 8000)
        results = orchestrator.check_invariants()
        # All should pass when state is consistent
        for r in results:
            assert r.passed is True

    async def test_invariant_catches_stale_offload(self, orchestrator):
        # Simulate inconsistent state: offload active but no reachable node
        results = orchestrator.check_invariants(overrides={
            "gcp_offload_active": True,
            "gcp_node_ip": None,
            "gcp_node_reachable": False,
        })
        failed = [r for r in results if not r.passed]
        assert len(failed) >= 1


class TestBudgetIntegration:

    async def test_acquire_budget_slot(self, orchestrator):
        from backend.core.startup_concurrency_budget import HeavyTaskCategory
        async with orchestrator.budget_acquire(HeavyTaskCategory.MODEL_LOAD, "prime"):
            assert orchestrator.budget_active_count >= 1

    async def test_budget_history_after_release(self, orchestrator):
        from backend.core.startup_concurrency_budget import HeavyTaskCategory
        async with orchestrator.budget_acquire(HeavyTaskCategory.MODEL_LOAD, "prime"):
            await asyncio.sleep(0.01)
        assert len(orchestrator.budget_history) == 1


class TestTelemetryEmission:

    async def test_phase_resolution_emits_event(self, orchestrator):
        await orchestrator.resolve_phase("PREWARM_GCP")
        events = orchestrator.event_history
        phase_events = [e for e in events if e.event_type == "phase_gate"]
        assert len(phase_events) == 1
        assert phase_events[0].detail["status"] == "passed"
