"""Integration tests for Disease 10 wiring — full boot sequence.

Disease 10 Wiring, Task 9.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.startup_config import load_startup_config
from backend.core.startup_orchestrator import StartupOrchestrator
from backend.core.startup_phase_gate import GateStatus
from backend.core.startup_routing_policy import BootRoutingDecision
from backend.core.routing_authority_fsm import AuthorityState
from backend.core.gcp_readiness_lease import HandshakeResult, HandshakeStep
from backend.core.startup_concurrency_budget import HeavyTaskCategory
from unittest.mock import MagicMock


class AlwaysPassProber:
    async def probe_health(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)
    async def probe_capabilities(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
    async def probe_warm_model(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)


class AlwaysFailProber:
    async def probe_health(self, host, port, timeout):
        from backend.core.gcp_readiness_lease import ReadinessFailureClass
        return HandshakeResult(
            step=HandshakeStep.HEALTH, passed=False,
            failure_class=ReadinessFailureClass.NETWORK, detail="unreachable",
        )
    async def probe_capabilities(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
    async def probe_warm_model(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)


class TestFullBootWithGCP:
    """Scenario: Normal boot with GCP available."""

    async def test_full_boot_sequence(self):
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        # Phase 1: GCP prewarm
        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.resolve_phase("PREWARM_GCP", detail="gcp ready")

        # Phase 2: Core services
        await orch.resolve_phase("CORE_SERVICES", detail="backend + intelligence up")

        # Phase 3: Core ready — triggers handoff eligibility
        await orch.resolve_phase("CORE_READY", detail="all core up")

        # Budget-wrapped heavy task
        async with orch.budget_acquire(HeavyTaskCategory.MODEL_LOAD, "prime"):
            await asyncio.sleep(0.01)

        # Attempt handoff
        mock_hybrid = MagicMock()
        mock_hybrid.is_disease10_active = False
        orch.set_hybrid_router(mock_hybrid)
        result = await orch.attempt_handoff()
        assert result.success is True
        assert orch.authority_state == AuthorityState.HYBRID_ACTIVE

        # Phase 4: Deferred components
        await orch.resolve_phase("DEFERRED_COMPONENTS")

        # Verify telemetry
        events = orch.event_history
        assert len(events) >= 5  # at minimum: 4 phases + 1 handoff


class TestFullBootWithoutGCP:
    """Scenario: Normal boot, GCP unavailable."""

    async def test_boot_without_gcp(self):
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysFailProber())

        # GCP lease fails
        success = await orch.acquire_gcp_lease("10.0.0.1", 8000)
        assert success is False

        # Revoke lease so routing policy falls back from PENDING
        orch.revoke_gcp_lease("handshake failed — health probe unreachable")

        # Skip prewarm gate
        await orch.skip_phase("PREWARM_GCP", reason="gcp unavailable")

        # Continue boot on fallback
        await orch.resolve_phase("CORE_SERVICES")
        await orch.resolve_phase("CORE_READY")

        decision, reason = orch.routing_decide()
        assert decision in (BootRoutingDecision.LOCAL_MINIMAL, BootRoutingDecision.CLOUD_CLAUDE)


class TestBudgetSerializesHeavyTasks:
    """Scenario: Budget prevents simultaneous heavy tasks."""

    async def test_model_load_and_reactor_serialized(self):
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        order = []

        async def task(cat, name, delay):
            async with orch.budget_acquire(cat, name):
                order.append(f"{name}_start")
                await asyncio.sleep(delay)
                order.append(f"{name}_end")

        t1 = asyncio.create_task(task(HeavyTaskCategory.MODEL_LOAD, "model", 0.05))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(task(HeavyTaskCategory.REACTOR_LAUNCH, "reactor", 0.01))
        await asyncio.gather(t1, t2)

        assert order == ["model_start", "model_end", "reactor_start", "reactor_end"]
