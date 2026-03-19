"""Tests for the StartupPhaseGate dependency-aware phase coordinator.

Disease 10 — Startup Sequencing, Task 1.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.startup_phase_gate import (
    GateEvent,
    GateFailureReason,
    GateResult,
    GateSnapshot,
    GateStatus,
    PhaseGateCoordinator,
    StartupPhase,
)


# ---------------------------------------------------------------------------
# TestStartupPhaseEnum
# ---------------------------------------------------------------------------

class TestStartupPhaseEnum:
    """Verify enum ordering and dependency graph."""

    def test_phases_are_ordered(self):
        phases = list(StartupPhase)
        assert phases == [
            StartupPhase.PREWARM_GCP,
            StartupPhase.CORE_SERVICES,
            StartupPhase.BOOT_CONTRACT_VALIDATION,
            StartupPhase.CORE_READY,
            StartupPhase.DEFERRED_COMPONENTS,
        ]

    def test_each_phase_has_dependencies(self):
        assert StartupPhase.PREWARM_GCP.dependencies == ()
        assert StartupPhase.CORE_SERVICES.dependencies == (StartupPhase.PREWARM_GCP,)
        assert StartupPhase.BOOT_CONTRACT_VALIDATION.dependencies == (StartupPhase.CORE_SERVICES,)
        assert StartupPhase.CORE_READY.dependencies == (StartupPhase.BOOT_CONTRACT_VALIDATION,)
        assert StartupPhase.DEFERRED_COMPONENTS.dependencies == (StartupPhase.CORE_READY,)


# ---------------------------------------------------------------------------
# TestGateCoordinatorBasic
# ---------------------------------------------------------------------------

class TestGateCoordinatorBasic:
    """Synchronous behaviour of the coordinator."""

    @pytest.fixture
    def coordinator(self) -> PhaseGateCoordinator:
        return PhaseGateCoordinator()

    def test_initial_status_is_pending(self, coordinator: PhaseGateCoordinator):
        for phase in StartupPhase:
            assert coordinator.status(phase) == GateStatus.PENDING

    def test_resolve_gate_succeeds(self, coordinator: PhaseGateCoordinator):
        # PREWARM_GCP has no deps — should resolve immediately.
        result = coordinator.resolve(StartupPhase.PREWARM_GCP, detail="gcp warm")
        assert result.status == GateStatus.PASSED
        assert result.phase == StartupPhase.PREWARM_GCP
        assert result.detail == "gcp warm"
        assert result.failure_reason is None
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.PASSED

    def test_resolve_with_unmet_dependency_fails(self, coordinator: PhaseGateCoordinator):
        # CORE_SERVICES depends on PREWARM_GCP which is still PENDING.
        result = coordinator.resolve(StartupPhase.CORE_SERVICES, detail="too early")
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.DEPENDENCY_UNMET
        assert coordinator.status(StartupPhase.CORE_SERVICES) == GateStatus.FAILED

    def test_resolve_chain(self, coordinator: PhaseGateCoordinator):
        # Walk the full dependency chain.
        r1 = coordinator.resolve(StartupPhase.PREWARM_GCP)
        assert r1.status == GateStatus.PASSED

        r2 = coordinator.resolve(StartupPhase.CORE_SERVICES)
        assert r2.status == GateStatus.PASSED

        r3 = coordinator.resolve(StartupPhase.BOOT_CONTRACT_VALIDATION)
        assert r3.status == GateStatus.PASSED

        r4 = coordinator.resolve(StartupPhase.CORE_READY)
        assert r4.status == GateStatus.PASSED

        r5 = coordinator.resolve(StartupPhase.DEFERRED_COMPONENTS)
        assert r5.status == GateStatus.PASSED

    def test_skip_gate(self, coordinator: PhaseGateCoordinator):
        result = coordinator.skip(StartupPhase.PREWARM_GCP, reason="not needed")
        assert result.status == GateStatus.SKIPPED
        assert result.detail == "not needed"
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.SKIPPED

        # A skipped dependency still lets the next phase resolve.
        r2 = coordinator.resolve(StartupPhase.CORE_SERVICES)
        assert r2.status == GateStatus.PASSED

    def test_fail_gate(self, coordinator: PhaseGateCoordinator):
        result = coordinator.fail(
            StartupPhase.PREWARM_GCP,
            reason=GateFailureReason.NETWORK_ERROR,
            detail="timeout reaching GCP",
        )
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.NETWORK_ERROR
        assert result.detail == "timeout reaching GCP"
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.FAILED

    def test_resolve_after_fail_is_rejected(self, coordinator: PhaseGateCoordinator):
        """Once a gate is in a terminal state, further mutations are no-ops."""
        coordinator.fail(
            StartupPhase.PREWARM_GCP,
            reason=GateFailureReason.NETWORK_ERROR,
            detail="net fail",
        )
        # Attempting to resolve a FAILED gate should return the original FAILED status.
        result = coordinator.resolve(StartupPhase.PREWARM_GCP, detail="retry")
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.NETWORK_ERROR
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.FAILED


# ---------------------------------------------------------------------------
# TestGateCoordinatorAsync
# ---------------------------------------------------------------------------

class TestGateCoordinatorAsync:
    """Async wait / timeout behaviour."""

    @pytest.fixture
    def coordinator(self) -> PhaseGateCoordinator:
        return PhaseGateCoordinator()

    async def test_wait_for_gate_resolves(self, coordinator: PhaseGateCoordinator):
        """Resolve a gate in the background after ~50 ms; waiter should unblock."""

        async def _resolve_later():
            await asyncio.sleep(0.05)
            coordinator.resolve(StartupPhase.PREWARM_GCP, detail="async")

        asyncio.create_task(_resolve_later())
        result = await coordinator.wait_for(StartupPhase.PREWARM_GCP, timeout=2.0)
        assert result.status == GateStatus.PASSED
        assert result.detail == "async"

    async def test_wait_for_timeout(self, coordinator: PhaseGateCoordinator):
        """Gate never resolves — should timeout and mark FAILED."""
        result = await coordinator.wait_for(StartupPhase.PREWARM_GCP, timeout=0.05)
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.TIMEOUT
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.FAILED

    async def test_wait_for_already_resolved(self, coordinator: PhaseGateCoordinator):
        """If gate is already resolved, wait_for returns instantly."""
        coordinator.resolve(StartupPhase.PREWARM_GCP, detail="pre-resolved")
        t0 = time.monotonic()
        result = await coordinator.wait_for(StartupPhase.PREWARM_GCP, timeout=5.0)
        elapsed = time.monotonic() - t0
        assert result.status == GateStatus.PASSED
        # Should return essentially immediately (< 50 ms).
        assert elapsed < 0.05


# ---------------------------------------------------------------------------
# TestGateCoordinatorObservability
# ---------------------------------------------------------------------------

class TestGateCoordinatorObservability:
    """Event log and snapshot introspection."""

    @pytest.fixture
    def coordinator(self) -> PhaseGateCoordinator:
        return PhaseGateCoordinator()

    def test_gate_events_are_recorded(self, coordinator: PhaseGateCoordinator):
        coordinator.resolve(StartupPhase.PREWARM_GCP, detail="warm")
        coordinator.fail(
            StartupPhase.CORE_SERVICES,
            reason=GateFailureReason.QUOTA_EXCEEDED,
            detail="quota hit",
        )

        events = coordinator.event_log
        assert len(events) == 2

        e0 = events[0]
        assert isinstance(e0, GateEvent)
        assert e0.phase == StartupPhase.PREWARM_GCP
        assert e0.new_status == GateStatus.PASSED
        assert e0.failure_reason is None
        assert e0.detail == "warm"
        assert isinstance(e0.timestamp, float)

        e1 = events[1]
        assert e1.phase == StartupPhase.CORE_SERVICES
        assert e1.new_status == GateStatus.FAILED
        assert e1.failure_reason == GateFailureReason.QUOTA_EXCEEDED

        # Returned list must be a copy — mutations should not affect internal state.
        events.clear()
        assert len(coordinator.event_log) == 2

    def test_snapshot_returns_all_statuses(self, coordinator: PhaseGateCoordinator):
        coordinator.resolve(StartupPhase.PREWARM_GCP, detail="ok")
        snap = coordinator.snapshot()

        assert set(snap.keys()) == set(StartupPhase)
        prewarm = snap[StartupPhase.PREWARM_GCP]
        assert isinstance(prewarm, GateSnapshot)
        assert prewarm.status == GateStatus.PASSED
        assert prewarm.timestamp is not None
        assert prewarm.failure_reason is None

        core_svc = snap[StartupPhase.CORE_SERVICES]
        assert core_svc.status == GateStatus.PENDING
        assert core_svc.timestamp is None
        assert core_svc.failure_reason is None
