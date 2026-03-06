"""Integration tests for Disease 10 recovery sequences.

Disease 10 Wiring, Task 9.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.startup_config import load_startup_config
from backend.core.startup_orchestrator import StartupOrchestrator
from backend.core.routing_authority_fsm import AuthorityState
from backend.core.startup_routing_policy import BootRoutingDecision
from backend.core.gcp_readiness_lease import HandshakeResult, HandshakeStep
from unittest.mock import MagicMock


class AlwaysPassProber:
    async def probe_health(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)
    async def probe_capabilities(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
    async def probe_warm_model(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)


class TestScenarioA_LeaseLossDuringBoot:
    """Lease revoked while still in BOOT_POLICY_ACTIVE."""

    async def test_lease_loss_during_boot(self):
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.resolve_phase("PREWARM_GCP")

        # Lease revoked mid-boot
        await orch.handle_lease_loss("spot_preemption")
        assert orch.lease_valid is False
        assert orch.authority_state == AuthorityState.BOOT_POLICY_ACTIVE

        # Routing falls back
        decision, reason = orch.routing_decide()
        assert decision != BootRoutingDecision.GCP_PRIME


class TestScenarioB_LeaseLossPostHandoff:
    """Lease revoked after handoff to HYBRID_ACTIVE."""

    async def test_lease_loss_post_handoff(self):
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.resolve_phase("PREWARM_GCP")
        await orch.resolve_phase("CORE_SERVICES")
        await orch.resolve_phase("CORE_READY")

        mock_hybrid = MagicMock()
        mock_hybrid.is_disease10_active = False
        orch.set_hybrid_router(mock_hybrid)
        await orch.attempt_handoff()
        assert orch.authority_state == AuthorityState.HYBRID_ACTIVE

        # Lease loss — should rollback authority
        await orch.handle_lease_loss("spot_preemption")
        assert orch.authority_state == AuthorityState.BOOT_POLICY_ACTIVE
        mock_hybrid.set_active.assert_called_with(False)


class TestScenarioC_HandoffFailure:
    """Handoff fails due to unmet guard."""

    async def test_handoff_failure_stays_in_boot(self):
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        await orch.resolve_phase("PREWARM_GCP")
        await orch.resolve_phase("CORE_SERVICES")
        # CORE_READY NOT resolved — handoff should fail

        result = await orch.attempt_handoff()
        assert result.success is False
        assert orch.authority_state == AuthorityState.BOOT_POLICY_ACTIVE

        # Boot continues on policy
        decision, reason = orch.routing_decide()
        assert decision is not None  # still functional
