"""Acceptance test matrix for Disease 10 Startup Sequencing.

Composes all 5 modules (Tasks 1-5) across 6 boot scenarios to verify
end-to-end integration and go/no-go readiness.

Disease 10 — Startup Sequencing, Task 6.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from backend.core.startup_phase_gate import (
    GateFailureReason,
    GateStatus,
    PhaseGateCoordinator,
    StartupPhase,
)
from backend.core.gcp_readiness_lease import (
    GCPReadinessLease,
    HandshakeResult,
    HandshakeStep,
    LeaseStatus,
    ReadinessFailureClass,
    ReadinessProber,
)
from backend.core.startup_concurrency_budget import (
    HeavyTaskCategory,
    StartupConcurrencyBudget,
)
from backend.core.boot_invariants import BootInvariantChecker
from backend.core.startup_routing_policy import (
    BootRoutingDecision,
    FallbackReason,
    StartupRoutingPolicy,
)


# ---------------------------------------------------------------------------
# ScenarioProber — controllable ReadinessProber for acceptance scenarios
# ---------------------------------------------------------------------------


class ScenarioProber(ReadinessProber):
    """Controllable prober that implements ReadinessProber for acceptance tests.

    Parameters
    ----------
    health_ok:
        Whether the health probe should pass.
    caps_ok:
        Whether the capabilities probe should pass.
    warm_ok:
        Whether the warm-model probe should pass.
    health_delay:
        Simulated delay (seconds) before the health probe returns.
    failure_class:
        If set, overrides the failure class on any failing probe step.
    """

    def __init__(
        self,
        health_ok: bool = True,
        caps_ok: bool = True,
        warm_ok: bool = True,
        health_delay: float = 0.0,
        failure_class: Optional[ReadinessFailureClass] = None,
    ) -> None:
        self.health_ok = health_ok
        self.caps_ok = caps_ok
        self.warm_ok = warm_ok
        self.health_delay = health_delay
        self.failure_class = failure_class

    async def probe_health(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        if self.health_delay > 0:
            await asyncio.sleep(self.health_delay)
        if self.health_ok:
            return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)
        return HandshakeResult(
            step=HandshakeStep.HEALTH,
            passed=False,
            failure_class=self.failure_class or ReadinessFailureClass.NETWORK,
            detail="health probe failed",
        )

    async def probe_capabilities(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        if self.caps_ok:
            return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
        return HandshakeResult(
            step=HandshakeStep.CAPABILITIES,
            passed=False,
            failure_class=self.failure_class or ReadinessFailureClass.SCHEMA_MISMATCH,
            detail="capabilities probe failed",
        )

    async def probe_warm_model(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        if self.warm_ok:
            return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)
        return HandshakeResult(
            step=HandshakeStep.WARM_MODEL,
            passed=False,
            failure_class=self.failure_class or ReadinessFailureClass.RESOURCE,
            detail="warm model probe failed",
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _find_invariant(results, inv_id: str):
    """Locate an InvariantResult by its invariant_id."""
    for r in results:
        if r.invariant_id == inv_id:
            return r
    raise AssertionError(f"Invariant {inv_id!r} not found in results")


# ---------------------------------------------------------------------------
# Scenario 1: Normal boot — GCP available
# ---------------------------------------------------------------------------


class TestScenario1NormalBootGCPAvailable:
    """Normal boot where GCP is available and all systems nominal."""

    async def test_phase_gates_resolve_in_order(self):
        """All 4 phases resolve PASSED when walked in dependency order."""
        coord = PhaseGateCoordinator()

        results = []
        for phase in StartupPhase:
            result = coord.resolve(phase)
            results.append(result)

        for result in results:
            assert result.status == GateStatus.PASSED, (
                f"Phase {result.phase.value} should be PASSED, got {result.status.value}"
            )

        # Verify the event log recorded all 4 transitions
        assert len(coord.event_log) == 4

    async def test_gcp_lease_acquired(self):
        """Full 3-step handshake succeeds and lease is ACTIVE."""
        prober = ScenarioProber(health_ok=True, caps_ok=True, warm_ok=True)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)

        ok = await lease.acquire(host="10.128.0.2", port=8080, timeout_per_step=5.0)

        assert ok is True
        assert lease.status == LeaseStatus.ACTIVE
        assert lease.is_valid is True
        assert len(lease.handshake_log) == 3
        assert all(step.passed for step in lease.handshake_log)

    async def test_routing_selects_gcp(self):
        """With GCP ready and deadline not expired, routing selects GCP_PRIME."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0, cloud_fallback_enabled=True)
        policy.signal_gcp_ready("10.128.0.2", 8080)

        decision, reason = policy.decide()

        assert decision == BootRoutingDecision.GCP_PRIME
        assert reason == FallbackReason.NONE

    async def test_invariants_pass(self):
        """All boot invariants pass with a well-formed GCP state."""
        checker = BootInvariantChecker()
        state = {
            "routing_target": "gcp",
            "gcp_handshake_complete": True,
            "gcp_offload_active": True,
            "gcp_node_ip": "10.128.0.2",
            "gcp_node_reachable": True,
            "local_model_loaded": False,
            "cloud_fallback_enabled": True,
        }

        results = checker.check_all(state)
        for r in results:
            assert r.passed is True, (
                f"Invariant {r.invariant_id} failed: {r.detail}"
            )


# ---------------------------------------------------------------------------
# Scenario 2: Normal boot — No GCP
# ---------------------------------------------------------------------------


class TestScenario2NormalBootNoGCP:
    """Normal boot where GCP is unavailable; system falls back gracefully."""

    async def test_prewarm_gate_skipped(self):
        """Skipping PREWARM_GCP still allows CORE_SERVICES to resolve."""
        coord = PhaseGateCoordinator()

        coord.skip(StartupPhase.PREWARM_GCP, reason="no GCP configured")
        assert coord.status(StartupPhase.PREWARM_GCP) == GateStatus.SKIPPED

        result = coord.resolve(StartupPhase.CORE_SERVICES)
        assert result.status == GateStatus.PASSED

    async def test_routing_falls_to_cloud(self):
        """With deadline=0 and no local model, routing falls to CLOUD_CLAUDE."""
        policy = StartupRoutingPolicy(
            gcp_deadline_s=0.0,
            cloud_fallback_enabled=True,
        )

        decision, reason = policy.decide()

        assert decision == BootRoutingDecision.CLOUD_CLAUDE
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    async def test_invariants_pass_without_gcp(self):
        """Invariants pass when no GCP state is present but cloud is enabled."""
        checker = BootInvariantChecker()
        state = {
            "routing_target": "cloud",
            "gcp_handshake_complete": False,
            "gcp_offload_active": False,
            "gcp_node_ip": None,
            "gcp_node_reachable": False,
            "local_model_loaded": False,
            "cloud_fallback_enabled": True,
        }

        results = checker.check_all(state)
        for r in results:
            assert r.passed is True, (
                f"Invariant {r.invariant_id} failed: {r.detail}"
            )


# ---------------------------------------------------------------------------
# Scenario 3: GCP slow boot
# ---------------------------------------------------------------------------


class TestScenario3GCPSlowBoot:
    """GCP is slow to respond, causing timeouts and fallback behavior."""

    async def test_lease_timeout_on_slow_health(self):
        """Health probe that exceeds timeout_per_step causes TIMEOUT failure."""
        prober = ScenarioProber(
            health_ok=True,
            caps_ok=True,
            warm_ok=True,
            health_delay=10.0,
        )
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)

        ok = await lease.acquire(
            host="10.128.0.2", port=8080, timeout_per_step=0.05,
        )

        assert ok is False
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.TIMEOUT

    async def test_routing_falls_to_local_after_deadline(self):
        """With deadline=0 and local model loaded, routing selects LOCAL_MINIMAL."""
        policy = StartupRoutingPolicy(
            gcp_deadline_s=0.0,
            cloud_fallback_enabled=True,
        )
        policy.signal_local_model_loaded()

        decision, reason = policy.decide()

        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    async def test_prewarm_gate_timeout_then_skip(self):
        """wait_for times out on PREWARM_GCP, then skip allows CORE_SERVICES."""
        coord = PhaseGateCoordinator()

        # Wait for PREWARM_GCP with a very short timeout — it will fail
        result = await coord.wait_for(StartupPhase.PREWARM_GCP, timeout=0.01)
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.TIMEOUT

        # Even though PREWARM_GCP failed, we can create a new coordinator
        # that skips the gate to allow CORE_SERVICES to proceed.
        # In the real system, the orchestrator would detect the timeout
        # and use a fresh coordinator or handle the failed gate.
        # Here we test the pattern: skip the gate, then resolve downstream.
        coord2 = PhaseGateCoordinator()
        coord2.skip(StartupPhase.PREWARM_GCP, reason="timed out, skipping")
        result2 = coord2.resolve(StartupPhase.CORE_SERVICES)
        assert result2.status == GateStatus.PASSED


# ---------------------------------------------------------------------------
# Scenario 4: Spot preemption
# ---------------------------------------------------------------------------


class TestScenario4SpotPreemption:
    """GCP instance is preempted mid-session."""

    async def test_lease_revocation(self):
        """Acquired lease can be revoked; status becomes REVOKED, is_valid=False."""
        prober = ScenarioProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)

        ok = await lease.acquire(host="10.128.0.2", port=8080, timeout_per_step=5.0)
        assert ok is True
        assert lease.is_valid is True

        lease.revoke(reason="spot preemption")

        assert lease.status == LeaseStatus.REVOKED
        assert lease.is_valid is False

    async def test_routing_falls_back_without_restart(self):
        """GCP ready then revoked with local loaded -> LOCAL_MINIMAL."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0, cloud_fallback_enabled=True)

        # GCP was ready
        policy.signal_gcp_ready("10.128.0.2", 8080)
        policy.signal_local_model_loaded()

        # Then revoked (spot preemption)
        policy.signal_gcp_revoked("spot preemption")

        decision, reason = policy.decide()

        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_REVOKED

    async def test_invariant_catches_stale_offload(self):
        """INV-2 violation: offload_active=True but node unreachable."""
        checker = BootInvariantChecker()
        state = {
            "routing_target": "local",
            "gcp_handshake_complete": True,
            "gcp_offload_active": True,
            "gcp_node_ip": "10.128.0.2",
            "gcp_node_reachable": False,
            "local_model_loaded": True,
            "cloud_fallback_enabled": True,
        }

        results = checker.check_all(state)
        inv2 = _find_invariant(results, "INV-2")

        assert inv2.passed is False
        assert inv2.trace is not None
        assert "reachable" in inv2.trace.trigger.lower() or "reachable" in inv2.detail.lower()


# ---------------------------------------------------------------------------
# Scenario 5: Quota failure
# ---------------------------------------------------------------------------


class TestScenario5QuotaFailure:
    """GCP provisioning fails due to quota exhaustion."""

    async def test_lease_classifies_quota_failure(self):
        """Health probe fails with QUOTA failure class -> lease FAILED with QUOTA."""
        prober = ScenarioProber(
            health_ok=False,
            failure_class=ReadinessFailureClass.QUOTA,
        )
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)

        ok = await lease.acquire(host="10.128.0.2", port=8080, timeout_per_step=5.0)

        assert ok is False
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.QUOTA

    async def test_gate_fails_with_quota_reason(self):
        """Explicitly failing PREWARM_GCP with QUOTA_EXCEEDED is recorded in event log."""
        coord = PhaseGateCoordinator()

        result = coord.fail(
            StartupPhase.PREWARM_GCP,
            reason=GateFailureReason.QUOTA_EXCEEDED,
            detail="GCP quota exhausted",
        )

        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.QUOTA_EXCEEDED

        events = coord.event_log
        assert len(events) == 1
        assert events[0].failure_reason == GateFailureReason.QUOTA_EXCEEDED
        assert events[0].new_status == GateStatus.FAILED


# ---------------------------------------------------------------------------
# Scenario 6: Memory-stressed boot
# ---------------------------------------------------------------------------


class TestScenario6MemoryStressedBoot:
    """Boot under memory pressure where concurrency must be throttled."""

    async def test_budget_prevents_simultaneous_heavy_tasks(self):
        """With max_concurrent=1, 3 tasks serialize (peak_concurrent never exceeds 1)."""
        budget = StartupConcurrencyBudget(max_concurrent=1)
        order: list[str] = []

        async def heavy_task(name: str, category: HeavyTaskCategory):
            async with budget.acquire(category, name):
                order.append(f"start-{name}")
                await asyncio.sleep(0.01)
                order.append(f"end-{name}")

        # Launch 3 tasks concurrently — they must serialize
        await asyncio.gather(
            heavy_task("model", HeavyTaskCategory.MODEL_LOAD),
            heavy_task("gcp", HeavyTaskCategory.GCP_PROVISION),
            heavy_task("reactor", HeavyTaskCategory.REACTOR_LAUNCH),
        )

        assert budget.peak_concurrent == 1
        assert len(budget.history) == 3
        # Each task must fully complete (start+end) before the next starts
        for i in range(0, len(order) - 1, 2):
            assert order[i].startswith("start-")
            assert order[i + 1].startswith("end-")
            # The start and end should be for the same task
            task_name_start = order[i].removeprefix("start-")
            task_name_end = order[i + 1].removeprefix("end-")
            assert task_name_start == task_name_end

    async def test_health_endpoint_remains_responsive(self):
        """A lightweight coroutine completes while a heavy task holds the budget."""
        budget = StartupConcurrencyBudget(max_concurrent=1)
        health_responded = asyncio.Event()

        async def heavy_task():
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "big-model"):
                # Simulate heavy work — yield control points
                for _ in range(5):
                    await asyncio.sleep(0.01)

        async def health_check():
            """Simulates a /health endpoint that should not be blocked by budget."""
            await asyncio.sleep(0.02)  # Small delay to ensure heavy task is running
            # Health check does NOT need to acquire a budget slot —
            # it runs independently of the concurrency budget.
            health_responded.set()

        await asyncio.gather(heavy_task(), health_check())

        assert health_responded.is_set()

    async def test_full_boot_sequence_with_budget(self):
        """End-to-end: gates + budget + routing + invariants all composed together.

        Sequence:
        1. Acquire GCP within budget
        2. Resolve gates in order
        3. Routing selects GCP
        4. Invariants pass
        5. Deferred Reactor within budget
        6. All gates PASSED
        7. Budget history has 3 entries
        """
        coord = PhaseGateCoordinator()
        budget = StartupConcurrencyBudget(max_concurrent=2)
        checker = BootInvariantChecker()
        prober = ScenarioProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0, cloud_fallback_enabled=True)

        # --- Phase 1: PREWARM_GCP (GCP provisioning within budget) ---
        async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "gcp-lease"):
            ok = await lease.acquire(
                host="10.128.0.2", port=8080, timeout_per_step=5.0,
            )
            assert ok is True
            assert lease.is_valid is True

            # Signal routing policy
            policy.signal_gcp_ready("10.128.0.2", 8080)

        coord.resolve(StartupPhase.PREWARM_GCP, detail="GCP lease acquired")

        # --- Phase 2: CORE_SERVICES (model load within budget) ---
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "local-model"):
            await asyncio.sleep(0.01)  # Simulate model loading
            policy.signal_local_model_loaded()

        coord.resolve(StartupPhase.CORE_SERVICES, detail="core services ready")

        # --- Phase 3: CORE_READY (routing decision + invariant check) ---
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME
        assert reason == FallbackReason.NONE

        state = {
            "routing_target": "gcp",
            "gcp_handshake_complete": True,
            "gcp_offload_active": True,
            "gcp_node_ip": "10.128.0.2",
            "gcp_node_reachable": True,
            "local_model_loaded": True,
            "cloud_fallback_enabled": True,
        }
        inv_results = checker.check_all(state)
        for r in inv_results:
            assert r.passed is True, f"Invariant {r.invariant_id} failed: {r.detail}"

        coord.resolve(StartupPhase.CORE_READY, detail="routing and invariants verified")

        # --- Phase 4: DEFERRED_COMPONENTS (Reactor launch within budget) ---
        async with budget.acquire(HeavyTaskCategory.REACTOR_LAUNCH, "reactor"):
            await asyncio.sleep(0.01)  # Simulate reactor startup

        coord.resolve(StartupPhase.DEFERRED_COMPONENTS, detail="reactor launched")

        # --- Final assertions ---
        # All gates should be PASSED
        for phase in StartupPhase:
            assert coord.status(phase) == GateStatus.PASSED, (
                f"Phase {phase.value} should be PASSED"
            )

        # Budget history should have 3 entries (gcp-lease, local-model, reactor)
        assert len(budget.history) == 3
        history_names = [h.name for h in budget.history]
        assert "gcp-lease" in history_names
        assert "local-model" in history_names
        assert "reactor" in history_names
