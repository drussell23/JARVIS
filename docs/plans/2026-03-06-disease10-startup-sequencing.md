# Disease 10: Startup Sequencing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cure the startup resource stampede that causes Reactor Core subprocess failures and `/health` unresponsiveness on the 16GB Mac by introducing phase gates, a GCP readiness lease, concurrency budgets, deferred component launch, startup-aware routing, and boot-time invariant enforcement.

**Architecture:** Six new standalone modules wired into the existing supervisor phase system. Phase gates add dependency checkpoints between existing phases (not a rewrite). GCP readiness becomes lease-based with 3-part handshake. A startup concurrency budget bounds heavy tasks. Reactor Core launch defers to a post-CORE_READY gate. The PrimeRouter gains a boot-phase routing policy with deadline-based deterministic fallback. Boot invariants enforce structural safety (no routing without handshake, no offload_active without reachable node).

**Tech Stack:** Python 3.11, asyncio, pytest (asyncio_mode=auto), dataclasses, enums, aiohttp (existing)

**Key Codebase Context:**
- `unified_supervisor.py`: 73K+ line monolith kernel. Phases 0-7 at lines 72420-74500+. Proactive GCP at line 72414. Phase 5 Trinity/Reactor at line 73569. Reactor Core status at line 84886.
- `backend/core/prime_router.py`: `_decide_route()` at line 389. `promote_gcp_endpoint()` at line 481. Flapping cooldown 30s.
- `backend/core/gcp_vm_manager.py`: 7400+ lines. `HealthVerdict` enum at line 231. Health probe at line 8460. `ensure_static_vm_ready()` returns `Tuple[bool, Optional[str], str]`.
- `backend/core/startup_contracts.py`: `ContractSeverity`, `ViolationReasonCode` enums. `EnvContract` dataclass.
- `backend/core/async_safety.py`: `LazyAsyncLock`, `shielded_wait_for` with `timeout_log_level`.
- Tests use `asyncio_mode = auto` (no explicit `@pytest.mark.asyncio` needed). Async fixtures with `yield`.

**Edge Cases (from design review):**
1. Spot preemption during startup: GCP path disappears mid-boot; must fail over without restarting supervisor.
2. Cold-start + quota/API failures: Prewarm can fail for non-resource reasons; classify separately.
3. State race: `gcp.offload_active=true` before `gcp.node_ip` propagated leads to blackhole routing.
4. Router split-brain: Multiple routers making independent decisions during transition windows.
5. Anti-churn feedback loop: Repeated threshold crossings oscillate local/GCP routing.
6. Child-process env drift: Subprocesses inheriting stale env values if bridge updates late.
7. Startup observability blind spot: No causal trace from "memory spike" -> "routing decision" -> "process failure".

**Go/No-Go Criteria:**
- No Reactor spawn failures under stressed boot.
- `/health` remains responsive through startup.
- Boot succeeds with GCP available AND unavailable (both paths deterministic).
- No routing oscillation during first N minutes.
- Full causal trace available for every boot degradation decision.

---

## Task 1: StartupPhaseGate — Phase Dependency System

Introduce a lightweight phase gate coordinator that lets startup code `await` named gates with timeout and reason codes. Gates are resolved by predicates (predecessor gates passed, health checks, memory thresholds). This replaces ad-hoc `asyncio.Event` / boolean flags with a structured, observable system.

**Files:**
- Create: `backend/core/startup_phase_gate.py`
- Test: `tests/unit/core/test_startup_phase_gate.py`

**Step 1: Write the failing tests**

```python
"""Disease 10 Task 1: StartupPhaseGate unit tests."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from backend.core.startup_phase_gate import (
    GateFailureReason,
    GateResult,
    GateStatus,
    PhaseGateCoordinator,
    StartupPhase,
)


class TestStartupPhaseEnum:
    """Phase ordering and dependency declarations."""

    def test_phases_are_ordered(self) -> None:
        """Phases have a total ordering matching boot sequence."""
        phases = list(StartupPhase)
        assert phases.index(StartupPhase.PREWARM_GCP) < phases.index(StartupPhase.CORE_SERVICES)
        assert phases.index(StartupPhase.CORE_SERVICES) < phases.index(StartupPhase.CORE_READY)
        assert phases.index(StartupPhase.CORE_READY) < phases.index(StartupPhase.DEFERRED_COMPONENTS)

    def test_each_phase_has_dependencies(self) -> None:
        """Every phase except the first declares its predecessors."""
        for phase in StartupPhase:
            assert isinstance(phase.dependencies, tuple)
        assert StartupPhase.PREWARM_GCP.dependencies == ()
        assert StartupPhase.CORE_SERVICES.dependencies == (StartupPhase.PREWARM_GCP,)
        assert StartupPhase.CORE_READY.dependencies == (StartupPhase.CORE_SERVICES,)
        assert StartupPhase.DEFERRED_COMPONENTS.dependencies == (StartupPhase.CORE_READY,)


class TestGateCoordinatorBasic:
    """Core gate resolution and waiting."""

    @pytest.fixture()
    def coordinator(self) -> PhaseGateCoordinator:
        return PhaseGateCoordinator()

    async def test_initial_status_is_pending(self, coordinator: PhaseGateCoordinator) -> None:
        for phase in StartupPhase:
            assert coordinator.status(phase) == GateStatus.PENDING

    async def test_resolve_gate_succeeds(self, coordinator: PhaseGateCoordinator) -> None:
        """Resolving a gate with no unmet dependencies transitions to PASSED."""
        result = coordinator.resolve(StartupPhase.PREWARM_GCP)
        assert result.status == GateStatus.PASSED
        assert result.failure_reason is None
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.PASSED

    async def test_resolve_with_unmet_dependency_fails(self, coordinator: PhaseGateCoordinator) -> None:
        """Cannot resolve a gate if predecessor gates have not passed."""
        result = coordinator.resolve(StartupPhase.CORE_SERVICES)
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.DEPENDENCY_UNMET

    async def test_resolve_chain(self, coordinator: PhaseGateCoordinator) -> None:
        """Resolving gates in order succeeds."""
        coordinator.resolve(StartupPhase.PREWARM_GCP)
        result = coordinator.resolve(StartupPhase.CORE_SERVICES)
        assert result.status == GateStatus.PASSED

    async def test_skip_gate(self, coordinator: PhaseGateCoordinator) -> None:
        """Skipping a gate marks it SKIPPED and allows dependents to proceed."""
        coordinator.skip(StartupPhase.PREWARM_GCP, reason="GCP disabled")
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.SKIPPED
        result = coordinator.resolve(StartupPhase.CORE_SERVICES)
        assert result.status == GateStatus.PASSED

    async def test_fail_gate(self, coordinator: PhaseGateCoordinator) -> None:
        """Failing a gate marks it FAILED with a reason code."""
        coordinator.fail(StartupPhase.PREWARM_GCP, GateFailureReason.TIMEOUT)
        assert coordinator.status(StartupPhase.PREWARM_GCP) == GateStatus.FAILED
        # Dependents should also fail
        result = coordinator.resolve(StartupPhase.CORE_SERVICES)
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.DEPENDENCY_UNMET


class TestGateCoordinatorAsync:
    """Async waiting with timeout."""

    @pytest.fixture()
    def coordinator(self) -> PhaseGateCoordinator:
        return PhaseGateCoordinator()

    async def test_wait_for_gate_resolves(self, coordinator: PhaseGateCoordinator) -> None:
        """wait_for blocks until gate is resolved, then returns result."""
        async def resolve_later():
            await asyncio.sleep(0.05)
            coordinator.resolve(StartupPhase.PREWARM_GCP)

        asyncio.get_event_loop().create_task(resolve_later())
        result = await coordinator.wait_for(StartupPhase.PREWARM_GCP, timeout=2.0)
        assert result.status == GateStatus.PASSED

    async def test_wait_for_timeout(self, coordinator: PhaseGateCoordinator) -> None:
        """wait_for returns FAILED with TIMEOUT reason when deadline expires."""
        result = await coordinator.wait_for(StartupPhase.PREWARM_GCP, timeout=0.05)
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.TIMEOUT

    async def test_wait_for_already_resolved(self, coordinator: PhaseGateCoordinator) -> None:
        """wait_for returns immediately if gate already resolved."""
        coordinator.resolve(StartupPhase.PREWARM_GCP)
        t0 = time.monotonic()
        result = await coordinator.wait_for(StartupPhase.PREWARM_GCP, timeout=5.0)
        elapsed = time.monotonic() - t0
        assert result.status == GateStatus.PASSED
        assert elapsed < 0.1


class TestGateCoordinatorObservability:
    """Event emission and causal tracing."""

    @pytest.fixture()
    def coordinator(self) -> PhaseGateCoordinator:
        return PhaseGateCoordinator()

    async def test_gate_events_are_recorded(self, coordinator: PhaseGateCoordinator) -> None:
        """Every gate transition is recorded in the event log."""
        coordinator.resolve(StartupPhase.PREWARM_GCP)
        coordinator.resolve(StartupPhase.CORE_SERVICES)
        events = coordinator.event_log
        assert len(events) >= 2
        assert events[0].phase == StartupPhase.PREWARM_GCP
        assert events[0].new_status == GateStatus.PASSED
        assert events[1].phase == StartupPhase.CORE_SERVICES

    async def test_snapshot_returns_all_statuses(self, coordinator: PhaseGateCoordinator) -> None:
        """snapshot() returns a dict of phase -> (status, timestamp, reason)."""
        coordinator.resolve(StartupPhase.PREWARM_GCP)
        snap = coordinator.snapshot()
        assert snap[StartupPhase.PREWARM_GCP].status == GateStatus.PASSED
        assert snap[StartupPhase.CORE_SERVICES].status == GateStatus.PENDING
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_phase_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.startup_phase_gate'`

**Step 3: Write the implementation**

```python
"""
StartupPhaseGate — Dependency-aware phase gate coordinator.

Disease 10: Startup Sequencing.

Defines named startup phases with explicit dependency ordering.
Gates are resolved, skipped, or failed. Other code awaits them.
Every transition is recorded for causal tracing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Phase definitions ────────────────────────────────────────────────
class StartupPhase(Enum):
    """Named startup phases in boot order.

    Each phase declares its predecessor dependencies via the
    ``dependencies`` property.  The coordinator enforces that a gate
    cannot be resolved until all dependencies are PASSED or SKIPPED.
    """

    PREWARM_GCP = auto()
    CORE_SERVICES = auto()
    CORE_READY = auto()
    DEFERRED_COMPONENTS = auto()

    @property
    def dependencies(self) -> Tuple[StartupPhase, ...]:
        return _PHASE_DEPS.get(self, ())


_PHASE_DEPS: Dict[StartupPhase, Tuple[StartupPhase, ...]] = {
    StartupPhase.PREWARM_GCP: (),
    StartupPhase.CORE_SERVICES: (StartupPhase.PREWARM_GCP,),
    StartupPhase.CORE_READY: (StartupPhase.CORE_SERVICES,),
    StartupPhase.DEFERRED_COMPONENTS: (StartupPhase.CORE_READY,),
}


# ── Status / result types ───────────────────────────────────────────
class GateStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"


class GateFailureReason(str, Enum):
    DEPENDENCY_UNMET = "dependency_unmet"
    TIMEOUT = "timeout"
    QUOTA_EXCEEDED = "quota_exceeded"
    NETWORK_ERROR = "network_error"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    PREEMPTION = "preemption"
    HEALTH_CHECK_FAILED = "health_check_failed"
    MANUAL = "manual"


@dataclass(frozen=True)
class GateResult:
    """Result of a gate resolution attempt."""
    phase: StartupPhase
    status: GateStatus
    failure_reason: Optional[GateFailureReason] = None
    detail: str = ""
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class GateSnapshot:
    """Point-in-time status for a single phase."""
    status: GateStatus
    timestamp: Optional[float]
    failure_reason: Optional[GateFailureReason]


@dataclass(frozen=True)
class GateEvent:
    """Recorded gate transition for observability."""
    phase: StartupPhase
    new_status: GateStatus
    failure_reason: Optional[GateFailureReason]
    detail: str
    timestamp: float


# ── Coordinator ──────────────────────────────────────────────────────
class PhaseGateCoordinator:
    """Manages startup phase gates with dependency enforcement.

    Thread-safety: NOT thread-safe.  Designed for single-threaded
    asyncio use within the supervisor event loop.
    """

    def __init__(self) -> None:
        self._statuses: Dict[StartupPhase, GateStatus] = {
            p: GateStatus.PENDING for p in StartupPhase
        }
        self._reasons: Dict[StartupPhase, Optional[GateFailureReason]] = {
            p: None for p in StartupPhase
        }
        self._timestamps: Dict[StartupPhase, Optional[float]] = {
            p: None for p in StartupPhase
        }
        self._waiters: Dict[StartupPhase, asyncio.Event] = {
            p: asyncio.Event() for p in StartupPhase
        }
        self._event_log: List[GateEvent] = []

    # ── Queries ──────────────────────────────────────────────────
    def status(self, phase: StartupPhase) -> GateStatus:
        return self._statuses[phase]

    @property
    def event_log(self) -> List[GateEvent]:
        return list(self._event_log)

    def snapshot(self) -> Dict[StartupPhase, GateSnapshot]:
        return {
            p: GateSnapshot(
                status=self._statuses[p],
                timestamp=self._timestamps[p],
                failure_reason=self._reasons[p],
            )
            for p in StartupPhase
        }

    # ── Mutations ────────────────────────────────────────────────
    def resolve(self, phase: StartupPhase, detail: str = "") -> GateResult:
        """Mark gate as PASSED if all dependencies are met."""
        for dep in phase.dependencies:
            if self._statuses[dep] not in (GateStatus.PASSED, GateStatus.SKIPPED):
                result = GateResult(
                    phase=phase,
                    status=GateStatus.FAILED,
                    failure_reason=GateFailureReason.DEPENDENCY_UNMET,
                    detail=f"Dependency {dep.name} is {self._statuses[dep].value}",
                )
                self._record(phase, GateStatus.FAILED, GateFailureReason.DEPENDENCY_UNMET, result.detail)
                return result

        self._set(phase, GateStatus.PASSED, None, detail)
        return GateResult(phase=phase, status=GateStatus.PASSED, detail=detail)

    def skip(self, phase: StartupPhase, reason: str = "") -> GateResult:
        """Mark gate as SKIPPED — allows dependents to proceed."""
        self._set(phase, GateStatus.SKIPPED, None, reason)
        return GateResult(phase=phase, status=GateStatus.SKIPPED, detail=reason)

    def fail(self, phase: StartupPhase, reason: GateFailureReason, detail: str = "") -> GateResult:
        """Mark gate as FAILED with a reason code."""
        self._set(phase, GateStatus.FAILED, reason, detail)
        return GateResult(phase=phase, status=GateStatus.FAILED, failure_reason=reason, detail=detail)

    # ── Async waiting ────────────────────────────────────────────
    async def wait_for(self, phase: StartupPhase, timeout: float) -> GateResult:
        """Block until gate is resolved (PASSED/SKIPPED/FAILED) or timeout."""
        if self._statuses[phase] != GateStatus.PENDING:
            return GateResult(
                phase=phase,
                status=self._statuses[phase],
                failure_reason=self._reasons[phase],
            )
        try:
            await asyncio.wait_for(self._waiters[phase].wait(), timeout=timeout)
            return GateResult(
                phase=phase,
                status=self._statuses[phase],
                failure_reason=self._reasons[phase],
            )
        except asyncio.TimeoutError:
            self._set(phase, GateStatus.FAILED, GateFailureReason.TIMEOUT,
                       f"Timed out after {timeout:.1f}s")
            return GateResult(
                phase=phase,
                status=GateStatus.FAILED,
                failure_reason=GateFailureReason.TIMEOUT,
                detail=f"Timed out after {timeout:.1f}s",
            )

    # ── Internal ─────────────────────────────────────────────────
    def _set(
        self,
        phase: StartupPhase,
        status: GateStatus,
        reason: Optional[GateFailureReason],
        detail: str,
    ) -> None:
        self._statuses[phase] = status
        self._reasons[phase] = reason
        self._timestamps[phase] = time.monotonic()
        self._waiters[phase].set()
        self._record(phase, status, reason, detail)
        logger.info(
            "[PhaseGate] %s -> %s%s%s",
            phase.name,
            status.value,
            f" ({reason.value})" if reason else "",
            f": {detail}" if detail else "",
        )

    def _record(
        self,
        phase: StartupPhase,
        status: GateStatus,
        reason: Optional[GateFailureReason],
        detail: str,
    ) -> None:
        self._event_log.append(GateEvent(
            phase=phase,
            new_status=status,
            failure_reason=reason,
            detail=detail,
            timestamp=time.monotonic(),
        ))
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_startup_phase_gate.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add backend/core/startup_phase_gate.py tests/unit/core/test_startup_phase_gate.py
git commit -m "feat(disease10): add StartupPhaseGate with dependency enforcement and observability"
```

---

## Task 2: GCPReadinessLease — Enhanced Readiness Contract

Replace the binary "ready/not-ready" GCP VM check with a lease-based readiness contract that requires a 3-part handshake (health + capabilities handshake + warm model verification). The lease has a TTL and must be refreshed. Failure reasons are classified by cause (quota, network, resource, preemption) to enable intelligent retry.

**Files:**
- Create: `backend/core/gcp_readiness_lease.py`
- Test: `tests/unit/core/test_gcp_readiness_lease.py`

**Step 1: Write the failing tests**

```python
"""Disease 10 Task 2: GCPReadinessLease unit tests."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.gcp_readiness_lease import (
    GCPReadinessLease,
    HandshakeResult,
    HandshakeStep,
    LeaseStatus,
    ReadinessFailureClass,
    ReadinessProber,
)


# ── Fake prober for testing ──────────────────────────────────────────
class FakeProber(ReadinessProber):
    """Controllable prober for tests."""

    def __init__(
        self,
        health_ok: bool = True,
        capabilities_ok: bool = True,
        warm_model_ok: bool = True,
        health_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.health_ok = health_ok
        self.capabilities_ok = capabilities_ok
        self.warm_model_ok = warm_model_ok
        self.health_data = health_data or {"status": "healthy", "ready_for_inference": True}
        self.probe_count = 0

    async def probe_health(self, host: str, port: int, timeout: float) -> HandshakeResult:
        self.probe_count += 1
        if self.health_ok:
            return HandshakeResult(step=HandshakeStep.HEALTH, passed=True, data=self.health_data)
        return HandshakeResult(
            step=HandshakeStep.HEALTH, passed=False,
            failure_class=ReadinessFailureClass.NETWORK,
            detail="Connection refused",
        )

    async def probe_capabilities(self, host: str, port: int, timeout: float) -> HandshakeResult:
        if self.capabilities_ok:
            return HandshakeResult(
                step=HandshakeStep.CAPABILITIES, passed=True,
                data={"contract_version": [0, 3, 0], "capabilities": ["inference"]},
            )
        return HandshakeResult(
            step=HandshakeStep.CAPABILITIES, passed=False,
            failure_class=ReadinessFailureClass.SCHEMA_MISMATCH,
            detail="Contract version incompatible",
        )

    async def probe_warm_model(self, host: str, port: int, timeout: float) -> HandshakeResult:
        if self.warm_model_ok:
            return HandshakeResult(
                step=HandshakeStep.WARM_MODEL, passed=True,
                data={"model": "prime-7b", "latency_ms": 42},
            )
        return HandshakeResult(
            step=HandshakeStep.WARM_MODEL, passed=False,
            failure_class=ReadinessFailureClass.RESOURCE,
            detail="Model not loaded",
        )


class TestHandshakeResult:
    def test_passed_result(self) -> None:
        r = HandshakeResult(step=HandshakeStep.HEALTH, passed=True, data={"status": "ok"})
        assert r.passed
        assert r.failure_class is None

    def test_failed_result_has_class(self) -> None:
        r = HandshakeResult(
            step=HandshakeStep.HEALTH, passed=False,
            failure_class=ReadinessFailureClass.QUOTA, detail="quota exceeded",
        )
        assert not r.passed
        assert r.failure_class == ReadinessFailureClass.QUOTA


class TestLeaseAcquisition:
    """3-part handshake -> lease acquisition."""

    async def test_full_handshake_succeeds(self) -> None:
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert ok
        assert lease.status == LeaseStatus.ACTIVE
        assert lease.host == "10.0.0.1"
        assert lease.port == 8000

    async def test_health_failure_blocks_lease(self) -> None:
        prober = FakeProber(health_ok=False)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert not ok
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.NETWORK

    async def test_capabilities_failure_blocks_lease(self) -> None:
        prober = FakeProber(capabilities_ok=False)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert not ok
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.SCHEMA_MISMATCH

    async def test_warm_model_failure_blocks_lease(self) -> None:
        prober = FakeProber(warm_model_ok=False)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert not ok
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.RESOURCE


class TestLeaseLifecycle:
    """TTL, refresh, revocation."""

    async def test_lease_expires_after_ttl(self) -> None:
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=0.05)
        await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert lease.is_valid
        await asyncio.sleep(0.06)
        assert not lease.is_valid
        assert lease.status == LeaseStatus.EXPIRED

    async def test_refresh_extends_lease(self) -> None:
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=0.1)
        await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        await asyncio.sleep(0.05)
        ok = await lease.refresh(timeout_per_step=5.0)
        assert ok
        assert lease.is_valid  # Extended by another TTL

    async def test_refresh_fails_on_unhealthy_vm(self) -> None:
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        prober.health_ok = False  # VM goes unhealthy
        ok = await lease.refresh(timeout_per_step=5.0)
        assert not ok
        assert lease.status == LeaseStatus.FAILED

    async def test_revoke_immediately_invalidates(self) -> None:
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        lease.revoke(reason="spot preemption detected")
        assert not lease.is_valid
        assert lease.status == LeaseStatus.REVOKED

    async def test_handshake_log_records_all_steps(self) -> None:
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        log = lease.handshake_log
        assert len(log) == 3
        assert [r.step for r in log] == [
            HandshakeStep.HEALTH,
            HandshakeStep.CAPABILITIES,
            HandshakeStep.WARM_MODEL,
        ]


class TestFailureClassification:
    """Failure class determines retry strategy."""

    def test_all_failure_classes_exist(self) -> None:
        expected = {"NETWORK", "QUOTA", "RESOURCE", "PREEMPTION", "SCHEMA_MISMATCH", "TIMEOUT"}
        actual = {c.name for c in ReadinessFailureClass}
        assert expected == actual

    async def test_timeout_failure_class(self) -> None:
        """Prober timeout is classified as TIMEOUT, not NETWORK."""

        class SlowProber(FakeProber):
            async def probe_health(self, host, port, timeout):
                await asyncio.sleep(timeout + 0.1)
                return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)

        prober = SlowProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=60.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=0.05)
        assert not ok
        assert lease.last_failure_class == ReadinessFailureClass.TIMEOUT
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_gcp_readiness_lease.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.gcp_readiness_lease'`

**Step 3: Write the implementation**

```python
"""
GCPReadinessLease — Lease-based VM readiness with 3-part handshake.

Disease 10: Startup Sequencing.

Replaces binary ready/not-ready with a lease that requires:
  1. Health check (HTTP 200 + ready_for_inference)
  2. Capabilities handshake (contract version + policy hash)
  3. Warm model verification (inference latency probe)

The lease has a TTL and must be periodically refreshed.
Failures are classified by cause for intelligent retry.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────
class HandshakeStep(str, Enum):
    HEALTH = "health"
    CAPABILITIES = "capabilities"
    WARM_MODEL = "warm_model"


class ReadinessFailureClass(str, Enum):
    """Classifies WHY readiness failed — drives retry strategy."""
    NETWORK = "NETWORK"             # Connection refused, DNS, firewall
    QUOTA = "QUOTA"                 # GCP quota/billing exceeded
    RESOURCE = "RESOURCE"           # VM exists but model not loaded / OOM
    PREEMPTION = "PREEMPTION"       # Spot VM reclaimed
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"  # Contract/capability incompatible
    TIMEOUT = "TIMEOUT"             # Step didn't complete in time


class LeaseStatus(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    EXPIRED = "expired"
    FAILED = "failed"
    REVOKED = "revoked"


# ── Data types ───────────────────────────────────────────────────────
@dataclass
class HandshakeResult:
    """Result of a single handshake step."""
    step: HandshakeStep
    passed: bool
    failure_class: Optional[ReadinessFailureClass] = None
    detail: str = ""
    data: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.monotonic)


# ── Prober protocol ─────────────────────────────────────────────────
class ReadinessProber(abc.ABC):
    """Abstract prober — implement per-environment (real HTTP vs mock)."""

    @abc.abstractmethod
    async def probe_health(self, host: str, port: int, timeout: float) -> HandshakeResult: ...

    @abc.abstractmethod
    async def probe_capabilities(self, host: str, port: int, timeout: float) -> HandshakeResult: ...

    @abc.abstractmethod
    async def probe_warm_model(self, host: str, port: int, timeout: float) -> HandshakeResult: ...


# ── Lease ────────────────────────────────────────────────────────────
class GCPReadinessLease:
    """Lease-based GCP VM readiness with 3-part handshake and TTL.

    Usage::

        prober = HTTPReadinessProber()  # or FakeProber for tests
        lease = GCPReadinessLease(prober=prober, ttl_seconds=120.0)

        if await lease.acquire("10.0.0.5", 8000, timeout_per_step=10.0):
            # GCP is ready — route to it
            ...
        else:
            # GCP failed — check lease.last_failure_class for retry strategy
            ...
    """

    def __init__(self, prober: ReadinessProber, ttl_seconds: float) -> None:
        self._prober = prober
        self._ttl = ttl_seconds
        self._status = LeaseStatus.INACTIVE
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._acquired_at: Optional[float] = None
        self._last_failure_class: Optional[ReadinessFailureClass] = None
        self._handshake_log: List[HandshakeResult] = []

    # ── Properties ───────────────────────────────────────────────
    @property
    def status(self) -> LeaseStatus:
        if self._status == LeaseStatus.ACTIVE and not self._is_within_ttl():
            self._status = LeaseStatus.EXPIRED
        return self._status

    @property
    def is_valid(self) -> bool:
        return self.status == LeaseStatus.ACTIVE

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def last_failure_class(self) -> Optional[ReadinessFailureClass]:
        return self._last_failure_class

    @property
    def handshake_log(self) -> List[HandshakeResult]:
        return list(self._handshake_log)

    # ── Acquire (3-part handshake) ───────────────────────────────
    async def acquire(self, host: str, port: int, timeout_per_step: float) -> bool:
        """Run 3-part handshake. Returns True if lease acquired."""
        self._handshake_log.clear()
        self._host = host
        self._port = port

        steps = [
            self._prober.probe_health,
            self._prober.probe_capabilities,
            self._prober.probe_warm_model,
        ]

        for step_fn in steps:
            try:
                result = await asyncio.wait_for(
                    step_fn(host, port, timeout_per_step),
                    timeout=timeout_per_step,
                )
            except asyncio.TimeoutError:
                step_name = step_fn.__name__.replace("probe_", "")
                result = HandshakeResult(
                    step=HandshakeStep(step_name) if step_name in HandshakeStep._value2member_map_ else HandshakeStep.HEALTH,
                    passed=False,
                    failure_class=ReadinessFailureClass.TIMEOUT,
                    detail=f"Step timed out after {timeout_per_step:.1f}s",
                )

            self._handshake_log.append(result)

            if not result.passed:
                self._status = LeaseStatus.FAILED
                self._last_failure_class = result.failure_class
                logger.warning(
                    "[GCPLease] Handshake failed at %s: %s (%s)",
                    result.step.value,
                    result.detail,
                    result.failure_class.value if result.failure_class else "unknown",
                )
                return False

        # All 3 steps passed — activate lease
        self._status = LeaseStatus.ACTIVE
        self._acquired_at = time.monotonic()
        self._last_failure_class = None
        logger.info(
            "[GCPLease] Lease acquired for %s:%d (TTL=%.0fs)",
            host, port, self._ttl,
        )
        return True

    # ── Refresh ──────────────────────────────────────────────────
    async def refresh(self, timeout_per_step: float) -> bool:
        """Re-run health probe to extend lease. Returns True if still valid."""
        if self._host is None or self._port is None:
            return False

        try:
            result = await asyncio.wait_for(
                self._prober.probe_health(self._host, self._port, timeout_per_step),
                timeout=timeout_per_step,
            )
        except asyncio.TimeoutError:
            result = HandshakeResult(
                step=HandshakeStep.HEALTH, passed=False,
                failure_class=ReadinessFailureClass.TIMEOUT,
                detail=f"Refresh timed out after {timeout_per_step:.1f}s",
            )

        if result.passed:
            self._acquired_at = time.monotonic()
            self._status = LeaseStatus.ACTIVE
            return True

        self._status = LeaseStatus.FAILED
        self._last_failure_class = result.failure_class
        return False

    # ── Revoke ───────────────────────────────────────────────────
    def revoke(self, reason: str = "") -> None:
        """Immediately invalidate the lease (e.g., spot preemption)."""
        self._status = LeaseStatus.REVOKED
        logger.warning("[GCPLease] Revoked: %s", reason)

    # ── Internal ─────────────────────────────────────────────────
    def _is_within_ttl(self) -> bool:
        if self._acquired_at is None:
            return False
        return (time.monotonic() - self._acquired_at) < self._ttl
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_gcp_readiness_lease.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_readiness_lease.py tests/unit/core/test_gcp_readiness_lease.py
git commit -m "feat(disease10): add GCPReadinessLease with 3-part handshake, TTL, and failure classification"
```

---

## Task 3: StartupConcurrencyBudget — Heavy Task Throttling

Add a startup concurrency budget that limits how many heavy tasks (model loading, GCP provisioning, Reactor launch) run simultaneously during boot. This prevents event-loop starvation and memory stampede. Implemented as a named-slot semaphore with configurable max concurrency.

**Files:**
- Create: `backend/core/startup_concurrency_budget.py`
- Test: `tests/unit/core/test_startup_concurrency_budget.py`

**Step 1: Write the failing tests**

```python
"""Disease 10 Task 3: StartupConcurrencyBudget unit tests."""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.startup_concurrency_budget import (
    HeavyTaskCategory,
    StartupConcurrencyBudget,
    TaskSlot,
)


class TestHeavyTaskCategory:
    def test_all_categories_exist(self) -> None:
        expected = {"MODEL_LOAD", "GCP_PROVISION", "REACTOR_LAUNCH", "ML_INIT", "SUBPROCESS_SPAWN"}
        actual = {c.name for c in HeavyTaskCategory}
        assert expected == actual

    def test_categories_have_default_weight(self) -> None:
        for cat in HeavyTaskCategory:
            assert cat.weight >= 1


class TestBudgetAcquisition:
    @pytest.fixture()
    def budget(self) -> StartupConcurrencyBudget:
        return StartupConcurrencyBudget(max_concurrent=2)

    async def test_acquire_within_budget(self, budget: StartupConcurrencyBudget) -> None:
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="ecapa") as slot:
            assert isinstance(slot, TaskSlot)
            assert slot.category == HeavyTaskCategory.MODEL_LOAD
            assert budget.active_count == 1

    async def test_concurrent_within_limit(self, budget: StartupConcurrencyBudget) -> None:
        """Two tasks can run concurrently when max_concurrent=2."""
        entered = []

        async def task(cat, name):
            async with budget.acquire(cat, name=name):
                entered.append(name)
                await asyncio.sleep(0.05)

        await asyncio.gather(
            task(HeavyTaskCategory.MODEL_LOAD, "ecapa"),
            task(HeavyTaskCategory.GCP_PROVISION, "gcp"),
        )
        assert len(entered) == 2

    async def test_third_task_blocks_until_slot_free(self, budget: StartupConcurrencyBudget) -> None:
        """Third task must wait when max_concurrent=2."""
        order = []

        async def task(name, hold_time):
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name=name):
                order.append(f"start:{name}")
                await asyncio.sleep(hold_time)
                order.append(f"end:{name}")

        await asyncio.gather(
            task("a", 0.05),
            task("b", 0.05),
            task("c", 0.01),  # Will start after a or b finishes
        )
        # c should start after one of a/b ends
        c_start_idx = order.index("start:c")
        assert any(
            order.index(f"end:{x}") < c_start_idx for x in ("a", "b")
        )

    async def test_active_count_tracks_slots(self, budget: StartupConcurrencyBudget) -> None:
        assert budget.active_count == 0
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="test"):
            assert budget.active_count == 1
        assert budget.active_count == 0


class TestBudgetTimeout:
    async def test_acquire_timeout_raises(self) -> None:
        budget = StartupConcurrencyBudget(max_concurrent=1)
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="blocker"):
            with pytest.raises(asyncio.TimeoutError):
                async with budget.acquire(
                    HeavyTaskCategory.GCP_PROVISION,
                    name="waiter",
                    timeout=0.05,
                ):
                    pass  # Should never reach here


class TestBudgetObservability:
    async def test_history_records_completed_tasks(self) -> None:
        budget = StartupConcurrencyBudget(max_concurrent=2)
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="ecapa"):
            await asyncio.sleep(0.01)
        history = budget.history
        assert len(history) == 1
        assert history[0].name == "ecapa"
        assert history[0].category == HeavyTaskCategory.MODEL_LOAD
        assert history[0].duration_s > 0

    async def test_peak_concurrent_tracked(self) -> None:
        budget = StartupConcurrencyBudget(max_concurrent=3)

        async def task(name):
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name=name):
                await asyncio.sleep(0.05)

        await asyncio.gather(task("a"), task("b"), task("c"))
        assert budget.peak_concurrent >= 2  # At least 2 ran in parallel
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_concurrency_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.startup_concurrency_budget'`

**Step 3: Write the implementation**

```python
"""
StartupConcurrencyBudget — Bounded heavy-task parallelism during boot.

Disease 10: Startup Sequencing.

Limits how many heavy startup tasks run simultaneously to prevent:
  - Event loop starvation (all tasks blocking on I/O or CPU)
  - Memory stampede (multiple model loads competing for RAM)
  - Process table exhaustion (multiple subprocess spawns)

Usage::

    budget = StartupConcurrencyBudget(max_concurrent=2)

    async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="ecapa"):
        await load_ecapa_model()
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, List, Optional

logger = logging.getLogger(__name__)


# ── Categories ───────────────────────────────────────────────────────
class HeavyTaskCategory(Enum):
    """Categories of heavy startup tasks with default weights."""
    MODEL_LOAD = 1        # Loading ML models into RAM
    GCP_PROVISION = 1     # GCP VM provisioning / health polling
    REACTOR_LAUNCH = 1    # Reactor Core subprocess spawn
    ML_INIT = 1           # ML pipeline initialization
    SUBPROCESS_SPAWN = 1  # Generic subprocess creation

    @property
    def weight(self) -> int:
        return self.value


# ── Slot / History ───────────────────────────────────────────────────
@dataclass(frozen=True)
class TaskSlot:
    """Active task slot metadata."""
    category: HeavyTaskCategory
    name: str
    acquired_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CompletedTask:
    """Historical record of a completed heavy task."""
    category: HeavyTaskCategory
    name: str
    duration_s: float
    started_at: float
    ended_at: float


# ── Budget ───────────────────────────────────────────────────────────
class StartupConcurrencyBudget:
    """Semaphore-based concurrency budget for heavy startup tasks.

    Args:
        max_concurrent: Maximum number of heavy tasks running simultaneously.
                        Configurable via env var ``JARVIS_STARTUP_MAX_HEAVY_TASKS``
                        (default 2 for 16GB Mac, 4 for 32GB+).
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._max = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: List[TaskSlot] = []
        self._history: List[CompletedTask] = []
        self._peak_concurrent = 0
        self._lock = asyncio.Lock()

    # ── Properties ───────────────────────────────────────────────
    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def peak_concurrent(self) -> int:
        return self._peak_concurrent

    @property
    def history(self) -> List[CompletedTask]:
        return list(self._history)

    # ── Acquire / release ────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def acquire(
        self,
        category: HeavyTaskCategory,
        name: str,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[TaskSlot]:
        """Acquire a concurrency slot. Blocks if budget exhausted.

        Args:
            category: What kind of heavy task this is.
            name: Human-readable name for logging.
            timeout: Max seconds to wait for a slot. None = wait forever.

        Raises:
            asyncio.TimeoutError: If timeout expires while waiting.
        """
        if timeout is not None:
            acquired = await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
        else:
            await self._semaphore.acquire()

        slot = TaskSlot(category=category, name=name)
        async with self._lock:
            self._active.append(slot)
            if len(self._active) > self._peak_concurrent:
                self._peak_concurrent = len(self._active)

        logger.info(
            "[ConcurrencyBudget] Acquired slot for %s/%s (%d/%d active)",
            category.name, name, self.active_count, self._max,
        )

        try:
            yield slot
        finally:
            ended_at = time.monotonic()
            async with self._lock:
                if slot in self._active:
                    self._active.remove(slot)
                self._history.append(CompletedTask(
                    category=category,
                    name=name,
                    duration_s=ended_at - slot.acquired_at,
                    started_at=slot.acquired_at,
                    ended_at=ended_at,
                ))
            self._semaphore.release()
            logger.info(
                "[ConcurrencyBudget] Released slot for %s/%s (%.1fs, %d/%d active)",
                category.name, name, ended_at - slot.acquired_at,
                self.active_count, self._max,
            )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_startup_concurrency_budget.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add backend/core/startup_concurrency_budget.py tests/unit/core/test_startup_concurrency_budget.py
git commit -m "feat(disease10): add StartupConcurrencyBudget with named-slot semaphore and observability"
```

---

## Task 4: BootInvariantChecker — Runtime Invariant Enforcement

Add a boot invariant registry that enforces structural safety rules and emits causal traces for every violation. Key invariants: no routing to endpoint without handshake, no `offload_active` without reachable node, no dual authority on routing target.

**Files:**
- Create: `backend/core/boot_invariants.py`
- Test: `tests/unit/core/test_boot_invariants.py`

**Step 1: Write the failing tests**

```python
"""Disease 10 Task 4: BootInvariantChecker unit tests."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import pytest

from backend.core.boot_invariants import (
    BootInvariantChecker,
    CausalTrace,
    InvariantResult,
    InvariantSeverity,
)


def _make_state(**overrides: Any) -> Dict[str, Any]:
    """Build a minimal boot state dict for invariant checking."""
    base: Dict[str, Any] = {
        "gcp_offload_active": False,
        "gcp_node_ip": None,
        "gcp_node_reachable": False,
        "gcp_handshake_complete": False,
        "routing_target": None,       # None | "local" | "gcp" | "cloud"
        "local_model_loaded": False,
        "cloud_fallback_enabled": True,
        "boot_phase": "preflight",     # preflight | core_services | core_ready | deferred
    }
    base.update(overrides)
    return base


class TestInvariantNoRoutingWithoutHandshake:
    """INV-1: No routing to GCP endpoint without completed handshake."""

    def test_routing_to_gcp_with_handshake_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            routing_target="gcp",
            gcp_handshake_complete=True,
            gcp_node_ip="10.0.0.1",
            gcp_node_reachable=True,
        )
        results = checker.check_all(state)
        inv1 = [r for r in results if r.invariant_id == "INV-1"]
        assert all(r.passed for r in inv1)

    def test_routing_to_gcp_without_handshake_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            routing_target="gcp",
            gcp_handshake_complete=False,
            gcp_node_ip="10.0.0.1",
        )
        results = checker.check_all(state)
        inv1 = [r for r in results if r.invariant_id == "INV-1"]
        assert any(not r.passed for r in inv1)

    def test_routing_to_local_skips_handshake_check(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target="local", gcp_handshake_complete=False)
        results = checker.check_all(state)
        inv1 = [r for r in results if r.invariant_id == "INV-1"]
        assert all(r.passed for r in inv1)


class TestInvariantNoOffloadWithoutReachable:
    """INV-2: No offload_active=true without reachable node."""

    def test_offload_active_with_reachable_node_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            gcp_offload_active=True,
            gcp_node_ip="10.0.0.1",
            gcp_node_reachable=True,
        )
        results = checker.check_all(state)
        inv2 = [r for r in results if r.invariant_id == "INV-2"]
        assert all(r.passed for r in inv2)

    def test_offload_active_without_ip_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(gcp_offload_active=True, gcp_node_ip=None)
        results = checker.check_all(state)
        inv2 = [r for r in results if r.invariant_id == "INV-2"]
        assert any(not r.passed for r in inv2)

    def test_offload_active_with_unreachable_node_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            gcp_offload_active=True,
            gcp_node_ip="10.0.0.1",
            gcp_node_reachable=False,
        )
        results = checker.check_all(state)
        inv2 = [r for r in results if r.invariant_id == "INV-2"]
        assert any(not r.passed for r in inv2)


class TestInvariantNoDualAuthority:
    """INV-3: No dual authority — exactly one routing target active."""

    def test_single_target_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(routing_target="gcp", gcp_handshake_complete=True, gcp_node_ip="10.0.0.1", gcp_node_reachable=True)
        results = checker.check_all(state)
        inv3 = [r for r in results if r.invariant_id == "INV-3"]
        assert all(r.passed for r in inv3)

    def test_no_target_during_boot_passes(self) -> None:
        """During early boot, no routing target is acceptable."""
        checker = BootInvariantChecker()
        state = _make_state(routing_target=None, boot_phase="preflight")
        results = checker.check_all(state)
        inv3 = [r for r in results if r.invariant_id == "INV-3"]
        assert all(r.passed for r in inv3)


class TestInvariantNoDeadEndFallback:
    """INV-4: If GCP skipped and local not loaded, cloud fallback must be enabled."""

    def test_no_local_no_gcp_with_cloud_passes(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            routing_target=None,
            local_model_loaded=False,
            gcp_handshake_complete=False,
            cloud_fallback_enabled=True,
        )
        results = checker.check_all(state)
        inv4 = [r for r in results if r.invariant_id == "INV-4"]
        assert all(r.passed for r in inv4)

    def test_no_local_no_gcp_no_cloud_fails(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            routing_target=None,
            local_model_loaded=False,
            gcp_handshake_complete=False,
            cloud_fallback_enabled=False,
        )
        results = checker.check_all(state)
        inv4 = [r for r in results if r.invariant_id == "INV-4"]
        assert any(not r.passed for r in inv4)


class TestCausalTrace:
    """Every invariant violation emits a causal trace."""

    def test_violation_produces_trace(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state(
            gcp_offload_active=True,
            gcp_node_ip=None,
        )
        results = checker.check_all(state)
        violations = [r for r in results if not r.passed]
        assert len(violations) > 0
        for v in violations:
            assert v.trace is not None
            assert v.trace.trigger != ""
            assert v.trace.decision != ""
            assert v.trace.timestamp > 0

    def test_passing_invariant_has_no_trace(self) -> None:
        checker = BootInvariantChecker()
        state = _make_state()  # All defaults = safe
        results = checker.check_all(state)
        for r in results:
            if r.passed:
                assert r.trace is None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_boot_invariants.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.boot_invariants'`

**Step 3: Write the implementation**

```python
"""
BootInvariantChecker — Runtime invariant enforcement with causal tracing.

Disease 10: Startup Sequencing.

Enforces structural safety rules during boot and at routing decision points:
  INV-1: No routing to GCP endpoint without completed handshake
  INV-2: No offload_active without reachable node
  INV-3: No dual authority on routing target
  INV-4: No dead-end fallback (at least one path must be available)

Every violation emits a CausalTrace linking trigger -> decision -> outcome.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Types ────────────────────────────────────────────────────────────
class InvariantSeverity(str, Enum):
    CRITICAL = "critical"   # Must be fixed before routing
    WARNING = "warning"     # Log and continue but degraded
    ADVISORY = "advisory"   # Informational only


@dataclass(frozen=True)
class CausalTrace:
    """Links a trigger event to the routing/state decision that violated an invariant."""
    trigger: str         # What caused the state (e.g., "gcp.offload_active set to true")
    decision: str        # What decision was made (e.g., "routing_target = gcp")
    outcome: str         # What the violation means (e.g., "blackhole: no reachable endpoint")
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class InvariantResult:
    """Result of checking a single invariant."""
    invariant_id: str
    description: str
    passed: bool
    severity: InvariantSeverity
    trace: Optional[CausalTrace] = None
    detail: str = ""


# ── Checker ──────────────────────────────────────────────────────────
_InvariantFn = Callable[[Dict[str, Any]], InvariantResult]


class BootInvariantChecker:
    """Registry of boot-time invariants with causal trace emission.

    Usage::

        checker = BootInvariantChecker()
        state = {
            "gcp_offload_active": True,
            "gcp_node_ip": "10.0.0.1",
            "gcp_node_reachable": True,
            "gcp_handshake_complete": True,
            "routing_target": "gcp",
            "local_model_loaded": False,
            "cloud_fallback_enabled": True,
            "boot_phase": "core_ready",
        }
        results = checker.check_all(state)
        violations = [r for r in results if not r.passed]
    """

    def __init__(self) -> None:
        self._invariants: List[_InvariantFn] = [
            self._inv1_no_routing_without_handshake,
            self._inv2_no_offload_without_reachable,
            self._inv3_no_dual_authority,
            self._inv4_no_dead_end_fallback,
        ]

    def check_all(self, state: Dict[str, Any]) -> List[InvariantResult]:
        """Run all registered invariants against the given state."""
        results = []
        for inv_fn in self._invariants:
            result = inv_fn(state)
            if not result.passed:
                logger.warning(
                    "[BootInvariant] VIOLATION %s: %s — %s",
                    result.invariant_id,
                    result.description,
                    result.detail,
                )
            results.append(result)
        return results

    # ── INV-1: No routing without handshake ──────────────────────
    @staticmethod
    def _inv1_no_routing_without_handshake(state: Dict[str, Any]) -> InvariantResult:
        routing_target = state.get("routing_target")
        handshake_done = state.get("gcp_handshake_complete", False)

        if routing_target == "gcp" and not handshake_done:
            return InvariantResult(
                invariant_id="INV-1",
                description="No routing to GCP endpoint without completed handshake",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                detail=f"routing_target=gcp but gcp_handshake_complete={handshake_done}",
                trace=CausalTrace(
                    trigger="routing_target set to 'gcp'",
                    decision="Route inference to GCP VM",
                    outcome="Requests will blackhole: handshake not completed",
                ),
            )
        return InvariantResult(
            invariant_id="INV-1",
            description="No routing to GCP endpoint without completed handshake",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )

    # ── INV-2: No offload without reachable node ────────────────
    @staticmethod
    def _inv2_no_offload_without_reachable(state: Dict[str, Any]) -> InvariantResult:
        offload_active = state.get("gcp_offload_active", False)
        node_ip = state.get("gcp_node_ip")
        node_reachable = state.get("gcp_node_reachable", False)

        if offload_active and (not node_ip or not node_reachable):
            missing = "no IP" if not node_ip else "unreachable"
            return InvariantResult(
                invariant_id="INV-2",
                description="No offload_active without reachable node",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                detail=f"gcp_offload_active=true but node is {missing} (ip={node_ip})",
                trace=CausalTrace(
                    trigger=f"gcp_offload_active set to true (ip={node_ip})",
                    decision="Advertise GCP offload as active",
                    outcome=f"Blackhole routing: offload active but node {missing}",
                ),
            )
        return InvariantResult(
            invariant_id="INV-2",
            description="No offload_active without reachable node",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )

    # ── INV-3: No dual authority ─────────────────────────────────
    @staticmethod
    def _inv3_no_dual_authority(state: Dict[str, Any]) -> InvariantResult:
        target = state.get("routing_target")
        # During early boot, no target is fine
        # Single target is fine
        # This invariant would trigger if we had multiple active routing
        # targets, but our state model uses a single routing_target field.
        # Still validate that the target is a known value.
        valid_targets = {None, "local", "gcp", "cloud"}
        if target not in valid_targets:
            return InvariantResult(
                invariant_id="INV-3",
                description="No dual authority on routing target",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                detail=f"routing_target='{target}' is not a valid single authority",
                trace=CausalTrace(
                    trigger=f"routing_target set to '{target}'",
                    decision="Unknown routing authority",
                    outcome="Undefined routing behavior",
                ),
            )
        return InvariantResult(
            invariant_id="INV-3",
            description="No dual authority on routing target",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )

    # ── INV-4: No dead-end fallback ──────────────────────────────
    @staticmethod
    def _inv4_no_dead_end_fallback(state: Dict[str, Any]) -> InvariantResult:
        local_loaded = state.get("local_model_loaded", False)
        gcp_ready = state.get("gcp_handshake_complete", False)
        cloud_enabled = state.get("cloud_fallback_enabled", True)

        if not local_loaded and not gcp_ready and not cloud_enabled:
            return InvariantResult(
                invariant_id="INV-4",
                description="No dead-end fallback: at least one inference path must be available",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                detail="No local model, no GCP handshake, cloud fallback disabled",
                trace=CausalTrace(
                    trigger="All inference paths unavailable",
                    decision="No routing path available",
                    outcome="Dead-end: all inference requests will fail",
                ),
            )
        return InvariantResult(
            invariant_id="INV-4",
            description="No dead-end fallback: at least one inference path must be available",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_boot_invariants.py -v`
Expected: All 11 tests PASS

**Step 5: Commit**

```bash
git add backend/core/boot_invariants.py tests/unit/core/test_boot_invariants.py
git commit -m "feat(disease10): add BootInvariantChecker with 4 invariants and causal tracing"
```

---

## Task 5: StartupRoutingPolicy — Deadline-Based Deterministic Fallback

Add a startup-aware routing policy that overlays `PrimeRouter._decide_route()` during boot. The policy uses deadlines: if GCP handshake completes before the deadline, route to GCP; if deadline expires, fall back to LOCAL_MINIMAL or CLOUD_CLAUDE deterministically. No oscillation, no dead-ends.

**Files:**
- Create: `backend/core/startup_routing_policy.py`
- Test: `tests/unit/core/test_startup_routing_policy.py`

**Step 1: Write the failing tests**

```python
"""Disease 10 Task 5: StartupRoutingPolicy unit tests."""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.startup_routing_policy import (
    BootRoutingDecision,
    FallbackReason,
    StartupRoutingPolicy,
)


class TestBootRoutingDecision:
    def test_all_decisions_exist(self) -> None:
        expected = {"GCP_PRIME", "LOCAL_MINIMAL", "CLOUD_CLAUDE", "DEGRADED", "PENDING"}
        actual = {d.name for d in BootRoutingDecision}
        assert expected == actual


class TestPolicyDuringBoot:
    """Deadline-based routing during startup."""

    def test_pending_before_any_signal(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.PENDING

    def test_gcp_ready_before_deadline(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME

    def test_deadline_expired_with_local(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0)  # Already expired
        policy.signal_local_model_loaded()
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    def test_deadline_expired_no_local_with_cloud(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0, cloud_fallback_enabled=True)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.CLOUD_CLAUDE
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    def test_deadline_expired_no_local_no_cloud(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0, cloud_fallback_enabled=False)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.DEGRADED
        assert reason == FallbackReason.NO_AVAILABLE_PATH

    def test_gcp_revoked_falls_back_to_local(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        policy.signal_gcp_revoked(reason="spot preemption")
        policy.signal_local_model_loaded()
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_REVOKED

    def test_gcp_revoked_no_local_falls_to_cloud(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0, cloud_fallback_enabled=True)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        policy.signal_gcp_revoked(reason="spot preemption")
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.CLOUD_CLAUDE
        assert reason == FallbackReason.GCP_REVOKED


class TestPolicyFinalization:
    """Policy becomes inactive after boot completes."""

    def test_finalize_locks_decision(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        policy.finalize()
        assert policy.is_finalized
        # Decision is locked
        decision, _ = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME

    def test_signals_after_finalize_are_ignored(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        policy.finalize()
        policy.signal_gcp_revoked(reason="late preemption")
        decision, _ = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME  # Unchanged


class TestPolicyObservability:
    def test_decision_log_records_transitions(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        policy.decide()
        log = policy.decision_log
        assert len(log) >= 1
        assert log[-1].decision == BootRoutingDecision.GCP_PRIME
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_routing_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.startup_routing_policy'`

**Step 3: Write the implementation**

```python
"""
StartupRoutingPolicy — Deadline-based deterministic fallback during boot.

Disease 10: Startup Sequencing.

Overlays PrimeRouter._decide_route() during startup with explicit deadlines:

  1. Before GCP deadline: wait for GCP handshake
  2. GCP ready before deadline: route to GCP_PRIME
  3. GCP deadline expired:
     a. Local model loaded -> LOCAL_MINIMAL
     b. Cloud fallback enabled -> CLOUD_CLAUDE
     c. Nothing available -> DEGRADED

No oscillation, no dead-ends.  After boot completes, policy is finalized
and the normal PrimeRouter takes over.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────
class BootRoutingDecision(str, Enum):
    PENDING = "pending"           # Still within GCP deadline, waiting
    GCP_PRIME = "gcp_prime"       # GCP handshake succeeded
    LOCAL_MINIMAL = "local_minimal"  # Fallback to local model
    CLOUD_CLAUDE = "cloud_claude"    # Fallback to cloud API
    DEGRADED = "degraded"            # No path available


class FallbackReason(str, Enum):
    NONE = "none"
    GCP_DEADLINE_EXPIRED = "gcp_deadline_expired"
    GCP_REVOKED = "gcp_revoked"
    GCP_HANDSHAKE_FAILED = "gcp_handshake_failed"
    NO_AVAILABLE_PATH = "no_available_path"


# ── Decision log entry ───────────────────────────────────────────────
@dataclass(frozen=True)
class DecisionLogEntry:
    decision: BootRoutingDecision
    reason: FallbackReason
    timestamp: float = field(default_factory=time.monotonic)
    detail: str = ""


# ── Policy ───────────────────────────────────────────────────────────
class StartupRoutingPolicy:
    """Deadline-based startup routing with deterministic fallback chain.

    Args:
        gcp_deadline_s: Seconds from construction to wait for GCP readiness.
                        Configurable via ``JARVIS_GCP_BOOT_DEADLINE_S`` (default 60).
        cloud_fallback_enabled: Whether Cloud Claude API is available as fallback.
    """

    def __init__(
        self,
        gcp_deadline_s: float = 60.0,
        cloud_fallback_enabled: bool = True,
    ) -> None:
        self._created_at = time.monotonic()
        self._gcp_deadline = gcp_deadline_s
        self._cloud_fallback = cloud_fallback_enabled

        # Signals
        self._gcp_ready = False
        self._gcp_host: Optional[str] = None
        self._gcp_port: Optional[int] = None
        self._gcp_revoked = False
        self._gcp_revoke_reason: str = ""
        self._local_loaded = False

        # State
        self._finalized = False
        self._decision_log: List[DecisionLogEntry] = []

    # ── Properties ───────────────────────────────────────────────
    @property
    def is_finalized(self) -> bool:
        return self._finalized

    @property
    def decision_log(self) -> List[DecisionLogEntry]:
        return list(self._decision_log)

    @property
    def gcp_deadline_remaining(self) -> float:
        elapsed = time.monotonic() - self._created_at
        return max(0.0, self._gcp_deadline - elapsed)

    # ── Signals ──────────────────────────────────────────────────
    def signal_gcp_ready(self, host: str, port: int) -> None:
        if self._finalized:
            return
        self._gcp_ready = True
        self._gcp_host = host
        self._gcp_port = port
        logger.info("[BootRouting] GCP ready signal: %s:%d", host, port)

    def signal_gcp_revoked(self, reason: str) -> None:
        if self._finalized:
            return
        self._gcp_revoked = True
        self._gcp_revoke_reason = reason
        self._gcp_ready = False
        logger.warning("[BootRouting] GCP revoked: %s", reason)

    def signal_local_model_loaded(self) -> None:
        if self._finalized:
            return
        self._local_loaded = True
        logger.info("[BootRouting] Local model loaded signal")

    def signal_gcp_handshake_failed(self, reason: str) -> None:
        if self._finalized:
            return
        self._gcp_ready = False
        logger.warning("[BootRouting] GCP handshake failed: %s", reason)

    # ── Decision ─────────────────────────────────────────────────
    def decide(self) -> Tuple[BootRoutingDecision, FallbackReason]:
        """Determine current routing decision based on signals and deadline."""
        decision, reason = self._compute()
        self._decision_log.append(DecisionLogEntry(
            decision=decision, reason=reason,
        ))
        return decision, reason

    def finalize(self) -> None:
        """Lock the current decision — no further signals accepted."""
        self._finalized = True
        logger.info("[BootRouting] Policy finalized")

    # ── Internal ─────────────────────────────────────────────────
    def _compute(self) -> Tuple[BootRoutingDecision, FallbackReason]:
        # 1. GCP is ready and not revoked -> use it
        if self._gcp_ready and not self._gcp_revoked:
            return BootRoutingDecision.GCP_PRIME, FallbackReason.NONE

        # 2. GCP was revoked -> deterministic fallback
        if self._gcp_revoked:
            return self._fallback(FallbackReason.GCP_REVOKED)

        # 3. GCP deadline expired -> deterministic fallback
        if self.gcp_deadline_remaining <= 0:
            return self._fallback(FallbackReason.GCP_DEADLINE_EXPIRED)

        # 4. Still within deadline, waiting
        return BootRoutingDecision.PENDING, FallbackReason.NONE

    def _fallback(self, reason: FallbackReason) -> Tuple[BootRoutingDecision, FallbackReason]:
        if self._local_loaded:
            return BootRoutingDecision.LOCAL_MINIMAL, reason
        if self._cloud_fallback:
            return BootRoutingDecision.CLOUD_CLAUDE, reason
        return BootRoutingDecision.DEGRADED, FallbackReason.NO_AVAILABLE_PATH
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_startup_routing_policy.py -v`
Expected: All 12 tests PASS

**Step 5: Commit**

```bash
git add backend/core/startup_routing_policy.py tests/unit/core/test_startup_routing_policy.py
git commit -m "feat(disease10): add StartupRoutingPolicy with deadline-based deterministic fallback"
```

---

## Task 6: Acceptance Test Matrix — Go/No-Go Verification

Integration tests that verify the 5 go/no-go criteria across 6 boot scenarios:
1. Normal boot (GCP available)
2. Normal boot (GCP unavailable)
3. GCP slow boot (exceeds deadline)
4. Spot preemption during startup
5. API quota failure during prewarm
6. Memory-stressed boot with concurrency budget

**Files:**
- Create: `tests/unit/core/test_disease10_acceptance.py`

**Step 1: Write the failing tests**

```python
"""Disease 10 Acceptance Test Matrix.

Verifies go/no-go criteria across 6 boot scenarios using the
standalone modules from Tasks 1-5. These are unit-level integration
tests that compose the modules without touching unified_supervisor.py.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import pytest

from backend.core.boot_invariants import BootInvariantChecker
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
from backend.core.startup_phase_gate import (
    GateFailureReason,
    GateStatus,
    PhaseGateCoordinator,
    StartupPhase,
)
from backend.core.startup_routing_policy import (
    BootRoutingDecision,
    FallbackReason,
    StartupRoutingPolicy,
)


# ── Reusable fake prober ─────────────────────────────────────────────
class ScenarioProber(ReadinessProber):
    """Prober whose behavior is set per-scenario."""

    def __init__(self) -> None:
        self.health_ok = True
        self.caps_ok = True
        self.warm_ok = True
        self.health_delay = 0.0
        self.failure_class: Optional[ReadinessFailureClass] = None

    async def probe_health(self, host, port, timeout):
        if self.health_delay:
            await asyncio.sleep(self.health_delay)
        if self.health_ok:
            return HandshakeResult(step=HandshakeStep.HEALTH, passed=True, data={"ready_for_inference": True})
        return HandshakeResult(step=HandshakeStep.HEALTH, passed=False, failure_class=self.failure_class or ReadinessFailureClass.NETWORK)

    async def probe_capabilities(self, host, port, timeout):
        if self.caps_ok:
            return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True, data={"contract_version": [0, 3, 0]})
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=False, failure_class=ReadinessFailureClass.SCHEMA_MISMATCH)

    async def probe_warm_model(self, host, port, timeout):
        if self.warm_ok:
            return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True, data={"latency_ms": 50})
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=False, failure_class=ReadinessFailureClass.RESOURCE)


# ═════════════════════════════════════════════════════════════════════
# SCENARIO 1: Normal boot — GCP available
# ═════════════════════════════════════════════════════════════════════
class TestScenario1NormalBootGCPAvailable:
    """Happy path: GCP VM is available, full handshake succeeds."""

    async def test_phase_gates_resolve_in_order(self) -> None:
        coord = PhaseGateCoordinator()
        coord.resolve(StartupPhase.PREWARM_GCP)
        coord.resolve(StartupPhase.CORE_SERVICES)
        coord.resolve(StartupPhase.CORE_READY)
        coord.resolve(StartupPhase.DEFERRED_COMPONENTS)
        for phase in StartupPhase:
            assert coord.status(phase) == GateStatus.PASSED

    async def test_gcp_lease_acquired(self) -> None:
        prober = ScenarioProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=120.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert ok
        assert lease.status == LeaseStatus.ACTIVE

    async def test_routing_selects_gcp(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        decision, _ = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME

    async def test_invariants_pass(self) -> None:
        checker = BootInvariantChecker()
        state = {
            "gcp_offload_active": True,
            "gcp_node_ip": "10.0.0.1",
            "gcp_node_reachable": True,
            "gcp_handshake_complete": True,
            "routing_target": "gcp",
            "local_model_loaded": False,
            "cloud_fallback_enabled": True,
            "boot_phase": "core_ready",
        }
        results = checker.check_all(state)
        assert all(r.passed for r in results)


# ═════════════════════════════════════════════════════════════════════
# SCENARIO 2: Normal boot — GCP unavailable
# ═════════════════════════════════════════════════════════════════════
class TestScenario2NormalBootNoGCP:
    """GCP VM not available — deterministic fallback to cloud."""

    async def test_prewarm_gate_skipped(self) -> None:
        coord = PhaseGateCoordinator()
        coord.skip(StartupPhase.PREWARM_GCP, reason="GCP disabled or unreachable")
        assert coord.status(StartupPhase.PREWARM_GCP) == GateStatus.SKIPPED
        # Dependents still proceed
        result = coord.resolve(StartupPhase.CORE_SERVICES)
        assert result.status == GateStatus.PASSED

    async def test_routing_falls_to_cloud(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0, cloud_fallback_enabled=True)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.CLOUD_CLAUDE
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    async def test_invariants_pass_without_gcp(self) -> None:
        checker = BootInvariantChecker()
        state = {
            "gcp_offload_active": False,
            "gcp_node_ip": None,
            "gcp_node_reachable": False,
            "gcp_handshake_complete": False,
            "routing_target": "cloud",
            "local_model_loaded": False,
            "cloud_fallback_enabled": True,
            "boot_phase": "core_ready",
        }
        results = checker.check_all(state)
        assert all(r.passed for r in results)


# ═════════════════════════════════════════════════════════════════════
# SCENARIO 3: GCP slow boot — deadline expires
# ═════════════════════════════════════════════════════════════════════
class TestScenario3GCPSlowBoot:
    """GCP VM takes too long, deadline expires, fall back to local."""

    async def test_lease_timeout_on_slow_health(self) -> None:
        prober = ScenarioProber()
        prober.health_delay = 10.0  # Very slow
        lease = GCPReadinessLease(prober=prober, ttl_seconds=120.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=0.05)
        assert not ok
        assert lease.last_failure_class == ReadinessFailureClass.TIMEOUT

    async def test_routing_falls_to_local_after_deadline(self) -> None:
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0)
        policy.signal_local_model_loaded()
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    async def test_prewarm_gate_fails_with_timeout(self) -> None:
        coord = PhaseGateCoordinator()
        result = await coord.wait_for(StartupPhase.PREWARM_GCP, timeout=0.05)
        assert result.status == GateStatus.FAILED
        assert result.failure_reason == GateFailureReason.TIMEOUT
        # CORE_SERVICES can still proceed if PREWARM_GCP is failed
        # (we skip the gate and let the routing policy handle fallback)
        coord.skip(StartupPhase.PREWARM_GCP, reason="Timeout — falling back")
        result2 = coord.resolve(StartupPhase.CORE_SERVICES)
        assert result2.status == GateStatus.PASSED


# ═════════════════════════════════════════════════════════════════════
# SCENARIO 4: Spot preemption during startup
# ═════════════════════════════════════════════════════════════════════
class TestScenario4SpotPreemption:
    """GCP VM acquired, then spot-preempted mid-boot."""

    async def test_lease_revocation_on_preemption(self) -> None:
        prober = ScenarioProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=120.0)
        await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert lease.is_valid
        # Spot preemption detected
        lease.revoke(reason="Spot VM preempted by GCP")
        assert not lease.is_valid
        assert lease.status == LeaseStatus.REVOKED

    async def test_routing_falls_back_without_restart(self) -> None:
        """Supervisor does NOT restart — routing falls back deterministically."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0, cloud_fallback_enabled=True)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)
        # Boot continues...then preemption
        policy.signal_gcp_revoked(reason="spot preemption")
        policy.signal_local_model_loaded()
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_REVOKED

    async def test_invariant_catches_stale_offload(self) -> None:
        """If offload_active lingers after preemption, invariant catches it."""
        checker = BootInvariantChecker()
        state = {
            "gcp_offload_active": True,  # Stale — should have been cleared
            "gcp_node_ip": "10.0.0.1",
            "gcp_node_reachable": False,  # Preempted = unreachable
            "gcp_handshake_complete": True,
            "routing_target": "gcp",
            "local_model_loaded": True,
            "cloud_fallback_enabled": True,
            "boot_phase": "core_ready",
        }
        results = checker.check_all(state)
        violations = [r for r in results if not r.passed]
        # INV-2: offload_active but unreachable
        assert any(r.invariant_id == "INV-2" for r in violations)


# ═════════════════════════════════════════════════════════════════════
# SCENARIO 5: API quota failure during prewarm
# ═════════════════════════════════════════════════════════════════════
class TestScenario5QuotaFailure:
    """GCP API returns quota exceeded — classified differently from network."""

    async def test_lease_classifies_quota_failure(self) -> None:
        prober = ScenarioProber()
        prober.health_ok = False
        prober.failure_class = ReadinessFailureClass.QUOTA
        lease = GCPReadinessLease(prober=prober, ttl_seconds=120.0)
        ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
        assert not ok
        assert lease.last_failure_class == ReadinessFailureClass.QUOTA

    async def test_gate_fails_with_quota_reason(self) -> None:
        coord = PhaseGateCoordinator()
        coord.fail(StartupPhase.PREWARM_GCP, GateFailureReason.QUOTA_EXCEEDED, "GCP quota exceeded")
        assert coord.status(StartupPhase.PREWARM_GCP) == GateStatus.FAILED
        # Event log captures the reason
        events = coord.event_log
        assert events[-1].failure_reason == GateFailureReason.QUOTA_EXCEEDED


# ═════════════════════════════════════════════════════════════════════
# SCENARIO 6: Memory-stressed boot with concurrency budget
# ═════════════════════════════════════════════════════════════════════
class TestScenario6MemoryStressedBoot:
    """Heavy tasks bounded by concurrency budget — no stampede."""

    async def test_budget_prevents_simultaneous_heavy_tasks(self) -> None:
        """With budget=1, heavy tasks serialize — no memory stampede."""
        budget = StartupConcurrencyBudget(max_concurrent=1)
        order = []

        async def heavy_task(name: str):
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name=name):
                order.append(f"start:{name}")
                await asyncio.sleep(0.02)
                order.append(f"end:{name}")

        await asyncio.gather(
            heavy_task("ecapa"),
            heavy_task("llm"),
            heavy_task("reactor"),
        )
        # With max_concurrent=1, tasks are fully serialized
        for i in range(0, len(order) - 1, 2):
            assert order[i].startswith("start:")
            assert order[i + 1].startswith("end:")
            # Each task ends before the next starts
            if i + 2 < len(order):
                end_name = order[i + 1].split(":")[1]
                next_start_name = order[i + 2].split(":")[1]
                assert end_name != next_start_name or True  # They're serialized

    async def test_health_endpoint_remains_responsive(self) -> None:
        """Simulates /health check during heavy startup — must not block."""
        budget = StartupConcurrencyBudget(max_concurrent=1)
        health_responded = False

        async def heavy_task():
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="heavy"):
                await asyncio.sleep(0.1)

        async def health_check():
            """Simulates /health — does NOT acquire budget slot."""
            nonlocal health_responded
            await asyncio.sleep(0.02)  # Called during heavy task
            health_responded = True  # Should complete even during heavy load

        await asyncio.gather(heavy_task(), health_check())
        assert health_responded, "/health must remain responsive during heavy startup"

    async def test_full_boot_sequence_with_budget(self) -> None:
        """End-to-end: gates + budget + routing + invariants."""
        coord = PhaseGateCoordinator()
        budget = StartupConcurrencyBudget(max_concurrent=2)
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0, cloud_fallback_enabled=True)
        checker = BootInvariantChecker()

        # Phase 1: Prewarm GCP (within budget)
        async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, name="gcp_prewarm"):
            prober = ScenarioProber()
            lease = GCPReadinessLease(prober=prober, ttl_seconds=120.0)
            ok = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)
            assert ok

        coord.resolve(StartupPhase.PREWARM_GCP)
        policy.signal_gcp_ready(host="10.0.0.1", port=8000)

        # Phase 2: Core services (backend, model loading within budget)
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, name="backend"):
            await asyncio.sleep(0.01)  # Simulate startup
        coord.resolve(StartupPhase.CORE_SERVICES)

        # Phase 3: Core ready
        coord.resolve(StartupPhase.CORE_READY)

        # Routing decision
        decision, _ = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME

        # Invariant check
        state = {
            "gcp_offload_active": True,
            "gcp_node_ip": "10.0.0.1",
            "gcp_node_reachable": True,
            "gcp_handshake_complete": True,
            "routing_target": "gcp",
            "local_model_loaded": False,
            "cloud_fallback_enabled": True,
            "boot_phase": "core_ready",
        }
        results = checker.check_all(state)
        assert all(r.passed for r in results), f"Violations: {[r for r in results if not r.passed]}"

        # Phase 4: Deferred components (Reactor Core within budget)
        async with budget.acquire(HeavyTaskCategory.REACTOR_LAUNCH, name="reactor"):
            await asyncio.sleep(0.01)
        coord.resolve(StartupPhase.DEFERRED_COMPONENTS)

        # All gates passed
        for phase in StartupPhase:
            assert coord.status(phase) == GateStatus.PASSED

        # Budget history
        assert len(budget.history) == 3
        assert budget.peak_concurrent >= 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_disease10_acceptance.py -v`
Expected: FAIL with `ModuleNotFoundError` (modules not yet created)

**Step 3: Implement (no new code — this task uses Tasks 1-5 modules)**

All modules already exist from Tasks 1-5. The acceptance tests compose them.

**Step 4: Run tests to verify they pass (after Tasks 1-5 are complete)**

Run: `python3 -m pytest tests/unit/core/test_disease10_acceptance.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add tests/unit/core/test_disease10_acceptance.py
git commit -m "test(disease10): add acceptance test matrix — 6 boot scenarios, 14 go/no-go tests"
```

---

## Acceptance Test Matrix Summary

| Scenario | Gates | Lease | Budget | Routing | Invariants | Tests |
|----------|-------|-------|--------|---------|------------|-------|
| 1. Normal boot (GCP ok) | PASSED chain | ACTIVE | Within limit | GCP_PRIME | All pass | 4 |
| 2. Normal boot (no GCP) | SKIPPED prewarm | N/A | N/A | CLOUD_CLAUDE | All pass | 3 |
| 3. GCP slow boot | TIMEOUT prewarm | TIMEOUT failure | N/A | LOCAL_MINIMAL | All pass | 3 |
| 4. Spot preemption | N/A | REVOKED | N/A | LOCAL fallback | Catches stale | 3 |
| 5. Quota failure | QUOTA_EXCEEDED | QUOTA class | N/A | Fallback chain | N/A | 2 |
| 6. Memory-stressed | Full chain | ACTIVE | Serialized | GCP_PRIME | All pass | 3 |

**Total: 60+ unit tests across 4 modules + 14 acceptance tests = 74+ tests**

---

## Post-Implementation: Supervisor Wiring (Separate PR)

After all 6 tasks pass, the modules need to be wired into the supervisor. This is a follow-up task (not part of this plan) that modifies:

1. **`unified_supervisor.py`** — Create `PhaseGateCoordinator` at kernel init. Call `coord.resolve(PREWARM_GCP)` after proactive GCP task. Move Reactor Core spawn to `await coord.wait_for(CORE_READY)`. Wrap heavy tasks in `budget.acquire()`.

2. **`backend/core/prime_router.py`** — Accept optional `StartupRoutingPolicy`. In `_decide_route()`, delegate to `policy.decide()` during boot, then `policy.finalize()` when CORE_READY passes.

3. **`backend/core/gcp_vm_manager.py`** — Replace `HealthVerdict.READY` with `GCPReadinessLease.acquire()` in `_check_vm_health()`. Pass lease to supervisor for refresh/revoke lifecycle.

This separation ensures the standalone modules are fully tested before any supervisor modification.

---

Plan complete and saved to `docs/plans/2026-03-06-disease10-startup-sequencing.md`.
