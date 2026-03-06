"""Acceptance tests for Disease 10 wiring — go/no-go criteria.

Disease 10 Wiring, Task 10.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.startup_config import load_startup_config
from backend.core.startup_orchestrator import StartupOrchestrator
from backend.core.routing_authority_fsm import AuthorityState
from backend.core.startup_routing_policy import BootRoutingDecision
from backend.core.startup_phase_gate import GateStatus
from backend.core.startup_concurrency_budget import HeavyTaskCategory
from backend.core.gcp_readiness_lease import HandshakeResult, HandshakeStep, ReadinessFailureClass
from unittest.mock import MagicMock


class AlwaysPassProber:
    async def probe_health(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)
    async def probe_capabilities(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
    async def probe_warm_model(self, host, port, timeout):
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)


# --- Go/No-Go: Deterministic boot WITH GCP ---

class TestGoNoGo_DeterministicBootWithGCP:

    async def test_deterministic_routing_to_gcp(self):
        """Boot with GCP always routes to GCP_PRIME."""
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.resolve_phase("PREWARM_GCP")
        await orch.resolve_phase("CORE_SERVICES")
        await orch.resolve_phase("CORE_READY")

        decision, _ = orch.routing_decide()
        assert decision == BootRoutingDecision.GCP_PRIME

    async def test_handoff_completes_cleanly(self):
        """Authority transitions from boot policy to hybrid router."""
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.resolve_phase("PREWARM_GCP")
        await orch.resolve_phase("CORE_SERVICES")
        await orch.resolve_phase("CORE_READY")

        mock_hybrid = MagicMock()
        mock_hybrid.is_disease10_active = False
        orch.set_hybrid_router(mock_hybrid)

        result = await orch.attempt_handoff()
        assert result.success is True
        assert orch.authority_state == AuthorityState.HYBRID_ACTIVE


# --- Go/No-Go: Deterministic boot WITHOUT GCP ---

class TestGoNoGo_DeterministicBootWithoutGCP:

    async def test_deterministic_fallback_without_gcp(self):
        """Without GCP, boot deterministically falls to local/cloud."""
        cfg = load_startup_config()

        class FailProber:
            async def probe_health(self, host, port, timeout):
                return HandshakeResult(
                    step=HandshakeStep.HEALTH, passed=False,
                    failure_class=ReadinessFailureClass.NETWORK,
                )
            async def probe_capabilities(self, host, port, timeout):
                return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
            async def probe_warm_model(self, host, port, timeout):
                return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)

        orch = StartupOrchestrator(config=cfg, prober=FailProber())

        success = await orch.acquire_gcp_lease("10.0.0.1", 8000)
        assert success is False
        await orch.skip_phase("PREWARM_GCP", reason="gcp unavailable")

        orch.signal_local_model_loaded()
        decision, _ = orch.routing_decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL


# --- Go/No-Go: No routing oscillation ---

class TestGoNoGo_NoRoutingOscillation:

    async def test_single_authority_at_all_times(self):
        """Authority token is unique throughout boot sequence."""
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        # Track authority changes
        authorities = [orch.authority_state]

        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.resolve_phase("PREWARM_GCP")
        authorities.append(orch.authority_state)

        await orch.resolve_phase("CORE_SERVICES")
        await orch.resolve_phase("CORE_READY")
        authorities.append(orch.authority_state)

        mock_hybrid = MagicMock()
        mock_hybrid.is_disease10_active = False
        orch.set_hybrid_router(mock_hybrid)
        await orch.attempt_handoff()
        authorities.append(orch.authority_state)

        # Should be monotonic progression, no oscillation
        assert authorities == [
            AuthorityState.BOOT_POLICY_ACTIVE,
            AuthorityState.BOOT_POLICY_ACTIVE,
            AuthorityState.BOOT_POLICY_ACTIVE,
            AuthorityState.HYBRID_ACTIVE,
        ]


# --- Go/No-Go: No Reactor spawn failures under budget ---

class TestGoNoGo_NoReactorSpawnFailures:

    async def test_reactor_waits_for_model_load(self):
        """Reactor launch is serialized behind model load via hard budget."""
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        order = []

        async def model_load():
            async with orch.budget_acquire(HeavyTaskCategory.MODEL_LOAD, "prime"):
                order.append("model_start")
                await asyncio.sleep(0.03)
                order.append("model_end")

        async def reactor_launch():
            await asyncio.sleep(0.01)  # ensure model starts first
            async with orch.budget_acquire(HeavyTaskCategory.REACTOR_LAUNCH, "reactor"):
                order.append("reactor_start")
                await asyncio.sleep(0.01)
                order.append("reactor_end")

        await asyncio.gather(model_load(), reactor_launch())
        assert order == ["model_start", "model_end", "reactor_start", "reactor_end"]


# --- Go/No-Go: Full causal trace for degradation ---

class TestGoNoGo_CausalTrace:

    async def test_every_degradation_has_trace(self):
        """Every routing degradation decision has a causal event trail."""
        cfg = load_startup_config()

        class FailProber:
            async def probe_health(self, host, port, timeout):
                return HandshakeResult(
                    step=HandshakeStep.HEALTH, passed=False,
                    failure_class=ReadinessFailureClass.NETWORK,
                    detail="connection refused",
                )
            async def probe_capabilities(self, host, port, timeout):
                return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
            async def probe_warm_model(self, host, port, timeout):
                return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)

        orch = StartupOrchestrator(config=cfg, prober=FailProber())

        await orch.acquire_gcp_lease("10.0.0.1", 8000)
        await orch.skip_phase("PREWARM_GCP", reason="gcp health failed")

        # Every event should have trace_id and detail
        events = orch.event_history
        for evt in events:
            assert evt.trace_id is not None
            assert evt.detail is not None

        # Check that lease failure has failure information in events
        lease_events = [e for e in events if "lease" in e.event_type]
        # At minimum, there should be a lease-related event
        assert len(lease_events) >= 1

        # Verify the lease_probe event contains failure_class
        probe_events = [e for e in events if e.event_type == "lease_probe"]
        assert len(probe_events) >= 1
        for pe in probe_events:
            assert pe.detail.get("failure_class") is not None


# --- Go/No-Go: Health responsive through startup ---

class TestGoNoGo_HealthResponsive:

    async def test_health_check_during_budget_contention(self):
        """Health endpoint responds even when budget is fully occupied."""
        cfg = load_startup_config()
        orch = StartupOrchestrator(config=cfg, prober=AlwaysPassProber())

        health_ok = False

        async def heavy_task():
            async with orch.budget_acquire(HeavyTaskCategory.MODEL_LOAD, "prime"):
                await asyncio.sleep(0.1)

        async def health_check():
            nonlocal health_ok
            await asyncio.sleep(0.02)  # during heavy task
            # Orchestrator should still respond to state queries
            snap = orch.gate_snapshot()
            assert snap is not None
            health_ok = True

        await asyncio.gather(heavy_task(), health_check())
        assert health_ok is True
