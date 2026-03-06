# Disease 9: Cross-Repo Integration Test Harness — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a dual-mode (mock/real) cross-repo failure/recovery test harness with StateOracle, scoped fault injection, continuous invariants, and 10 MVP scenarios.

**Architecture:** Layered modules — data types first, then StateOracle, then ScopedFaultInjector wrapping existing FaultInjector, then InvariantRegistry, then HarnessOrchestrator, then scenarios. Each layer tested before the next builds on it.

**Tech Stack:** Python 3.11+, pytest, asyncio, existing `FaultInjector` from `tests/adversarial/fault_injector.py`, `OrchestrationJournal` from `backend/core/orchestration_journal.py`, `LifecycleEngine` from `backend/core/lifecycle_engine.py`, `KernelState`/`LifecycleEvent` from `backend/core/kernel_lifecycle_engine.py`, `RoutingDecision` from `backend/core/prime_router.py`.

**Design doc:** `docs/plans/2026-03-05-cross-repo-integration-tests-design.md`

---

## Context for Implementers

### Key Existing Files

| File | What It Provides |
|---|---|
| `tests/adversarial/fault_injector.py` | `FaultInjector`, `FaultType`, `FaultSpec`, `apply_fault`, `MockClock` |
| `backend/core/orchestration_journal.py` | `OrchestrationJournal`, `StaleEpochError` (fenced_write, get_all_component_states) |
| `backend/core/lifecycle_engine.py` | `ComponentDeclaration`, `ComponentLocality`, `VALID_TRANSITIONS`, `compute_waves`, `LifecycleEngine` (component-level) |
| `backend/core/kernel_lifecycle_engine.py` | `KernelState`, `LifecycleEvent`, `TransitionRecord`, `LifecycleEngine` (kernel-level) |
| `backend/core/prime_router.py` | `RoutingDecision` enum (LOCAL_PRIME, GCP_PRIME, CLOUD_CLAUDE, HYBRID, CACHED, DEGRADED) |
| `tests/adversarial/conftest.py` | `fault_injector`, `mock_clock`, `tmp_trace_dir` fixtures |
| `tests/integration/conftest.py` | `mock_health_server`, `mock_prime_client`, `started_event_bus` fixtures |
| `pytest.ini` | Markers: `unit`, `integration`, `integration_mock`, `integration_real` |

### Import Conventions

```python
# All new harness code lives under tests/harness/
# Tests live under tests/integration/cross_repo/
```

### pytest Configuration

`pytest.ini` already has `pythonpath = . backend` and `asyncio_mode = auto`. We need to add two new markers: `integration_mock` and `integration_real`.

---

## Task 1: Add Pytest Markers + Directory Scaffolding

**Files:**
- Modify: `pytest.ini:31-45`
- Create: `tests/harness/__init__.py`
- Create: `tests/harness/conftest.py`
- Create: `tests/integration/cross_repo/__init__.py`

**Step 1: Add markers to pytest.ini**

Read `pytest.ini` and add two new markers after the existing `integration` marker (around line 34):

```ini
    integration_mock: Mock-mode cross-repo integration tests (CI-safe, <60s)
    integration_real: Real-mode cross-repo integration tests (staging only)
```

**Step 2: Create directory scaffolding**

Create empty `__init__.py` files:
- `tests/harness/__init__.py` — empty
- `tests/integration/cross_repo/__init__.py` — empty

Create `tests/harness/conftest.py`:

```python
"""Cross-repo integration test harness fixtures."""
```

**Step 3: Verify pytest discovers the new markers**

Run: `python3 -m pytest --markers | grep integration`
Expected: Lines for `integration`, `integration_mock`, `integration_real`

**Step 4: Commit**

```bash
git add pytest.ini tests/harness/__init__.py tests/harness/conftest.py tests/integration/cross_repo/__init__.py
git commit -m "feat(disease9): add pytest markers and directory scaffolding for cross-repo harness"
```

---

## Task 2: Core Data Types (ObservedEvent, OracleObservation, FaultScope, ContractStatus)

**Files:**
- Create: `tests/harness/types.py`
- Create: `tests/unit/harness/__init__.py`
- Create: `tests/unit/harness/test_types.py`

**Step 1: Write the failing test**

Create `tests/unit/harness/__init__.py` (empty).

Create `tests/unit/harness/test_types.py`:

```python
"""Tests for cross-repo harness data types."""
import time
import pytest


class TestObservedEvent:
    def test_frozen(self):
        from tests.harness.types import ObservedEvent
        event = ObservedEvent(
            oracle_event_seq=1, timestamp_mono=100.0,
            source="test", event_type="state_change",
            component="prime", old_value="READY", new_value="FAILED",
            epoch=1, scenario_phase="inject",
            trace_root_id="root123", trace_id="fault456",
            metadata={},
        )
        with pytest.raises(AttributeError):
            event.source = "changed"

    def test_all_fields_present(self):
        from tests.harness.types import ObservedEvent
        event = ObservedEvent(
            oracle_event_seq=42, timestamp_mono=1000.0,
            source="lifecycle_engine", event_type="recovery_started",
            component=None, old_value=None, new_value="STARTING",
            epoch=3, scenario_phase="recover",
            trace_root_id="r1", trace_id="t1",
            metadata={"scope": "component"},
        )
        assert event.oracle_event_seq == 42
        assert event.component is None
        assert event.metadata == {"scope": "component"}


class TestOracleObservation:
    def test_quality_values(self):
        from tests.harness.types import OracleObservation
        obs = OracleObservation(
            value="READY", observed_at_mono=100.0,
            observation_quality="fresh", source="health:prime",
        )
        assert obs.observation_quality == "fresh"

    def test_frozen(self):
        from tests.harness.types import OracleObservation
        obs = OracleObservation(
            value="FAILED", observed_at_mono=0.0,
            observation_quality="timeout", source="test",
        )
        with pytest.raises(AttributeError):
            obs.value = "READY"


class TestFaultScope:
    def test_all_scopes(self):
        from tests.harness.types import FaultScope
        assert len(FaultScope) == 5
        assert FaultScope.COMPONENT.value == "component"
        assert FaultScope.TRANSPORT.value == "transport"
        assert FaultScope.CONTRACT.value == "contract"
        assert FaultScope.CLOCK.value == "clock"
        assert FaultScope.PROCESS.value == "process"


class TestFaultComposition:
    def test_all_policies(self):
        from tests.harness.types import FaultComposition
        assert len(FaultComposition) == 3
        assert FaultComposition.REJECT.value == "reject"
        assert FaultComposition.STACK.value == "stack"
        assert FaultComposition.REPLACE.value == "replace"


class TestContractStatus:
    def test_compatible(self):
        from tests.harness.types import ContractStatus, ContractReasonCode
        status = ContractStatus(compatible=True, reason_code=ContractReasonCode.OK)
        assert status.compatible is True
        assert status.detail is None

    def test_incompatible_with_detail(self):
        from tests.harness.types import ContractStatus, ContractReasonCode
        status = ContractStatus(
            compatible=False,
            reason_code=ContractReasonCode.VERSION_WINDOW,
            detail="v2.1 vs v3.0",
        )
        assert status.compatible is False
        assert status.detail == "v2.1 vs v3.0"

    def test_all_reason_codes(self):
        from tests.harness.types import ContractReasonCode
        assert len(ContractReasonCode) == 6
        expected = {"ok", "version_window", "schema_hash",
                    "missing_capability", "handshake_missing", "handshake_expired"}
        assert {r.value for r in ContractReasonCode} == expected


class TestFaultHandle:
    def test_fields(self):
        import asyncio
        from tests.harness.types import FaultHandle, FaultScope

        async def noop():
            pass

        handle = FaultHandle(
            fault_id="abc123",
            scope=FaultScope.COMPONENT,
            target="prime",
            affected_components=frozenset({"prime"}),
            unaffected_components=frozenset({"backend", "trinity"}),
            pre_fault_baseline={"prime": "READY"},
            convergence_deadline_s=30.0,
            revert=noop,
        )
        assert handle.scope == FaultScope.COMPONENT
        assert "backend" in handle.unaffected_components
        assert handle.pre_fault_baseline == {"prime": "READY"}


class TestComponentStatus:
    def test_all_statuses(self):
        from tests.harness.types import ComponentStatus
        expected = {"READY", "DEGRADED", "FAILED", "LOST", "STOPPED",
                    "STARTING", "REGISTERED", "HANDSHAKING", "DRAINING",
                    "STOPPING", "UNKNOWN"}
        assert {s.value for s in ComponentStatus} == expected


class TestPhaseFailure:
    def test_typed_failure(self):
        from tests.harness.types import PhaseFailure
        f = PhaseFailure(
            phase="verify",
            failure_type="invariant_violation",
            detail="[epoch_monotonic] epoch decreased",
        )
        assert f.failure_type == "invariant_violation"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/harness/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.harness.types'`

**Step 3: Write minimal implementation**

Create `tests/harness/types.py`:

```python
"""Core data types for the cross-repo integration test harness.

Design doc: docs/plans/2026-03-05-cross-repo-integration-tests-design.md
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Literal, Optional


# ── Enums ────────────────────────────────────────────────────────────

class ComponentStatus(Enum):
    """Normalized component health status."""
    REGISTERED = "REGISTERED"
    STARTING = "STARTING"
    HANDSHAKING = "HANDSHAKING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    LOST = "LOST"
    UNKNOWN = "UNKNOWN"


class FaultScope(Enum):
    """Blast radius type for fault injections."""
    COMPONENT = "component"
    TRANSPORT = "transport"
    CONTRACT = "contract"
    CLOCK = "clock"
    PROCESS = "process"


class FaultComposition(Enum):
    """Policy for overlapping faults on the same target."""
    REJECT = "reject"
    STACK = "stack"
    REPLACE = "replace"


class ContractReasonCode(Enum):
    """Reason codes for contract compatibility status."""
    OK = "ok"
    VERSION_WINDOW = "version_window"
    SCHEMA_HASH = "schema_hash"
    MISSING_CAPABILITY = "missing_capability"
    HANDSHAKE_MISSING = "handshake_missing"
    HANDSHAKE_EXPIRED = "handshake_expired"


# ── Frozen Data Structures ───────────────────────────────────────────

@dataclass(frozen=True)
class ObservedEvent:
    """Provenance-tracked event in the oracle event log.

    oracle_event_seq is the total-order key assigned solely by the oracle.
    timestamp_mono is process-local monotonic time for SLO duration checks.
    """
    oracle_event_seq: int
    timestamp_mono: float
    source: str
    event_type: str
    component: Optional[str]
    old_value: Optional[str]
    new_value: str
    epoch: int
    scenario_phase: str
    trace_root_id: str
    trace_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OracleObservation:
    """Wrapper adding freshness metadata to every oracle read."""
    value: Any
    observed_at_mono: float
    observation_quality: Literal["fresh", "stale", "timeout", "divergent"]
    source: str


@dataclass(frozen=True)
class ContractStatus:
    """Contract compatibility status with reason taxonomy."""
    compatible: bool
    reason_code: ContractReasonCode
    detail: Optional[str] = None


@dataclass(frozen=True)
class FaultHandle:
    """Handle returned by ScopedFaultInjector for tracking and reverting faults."""
    fault_id: str
    scope: FaultScope
    target: str
    affected_components: FrozenSet[str]
    unaffected_components: FrozenSet[str]
    pre_fault_baseline: Dict[str, str]
    convergence_deadline_s: float
    revert: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class PhaseFailure:
    """Typed failure from a scenario phase for deterministic triage."""
    phase: str
    failure_type: str  # "phase_timeout" | "oracle_stale" | "invariant_violation" | "divergence_error"
    detail: str


@dataclass(frozen=True)
class PhaseResult:
    """Result of a single scenario phase execution."""
    duration_s: float
    violations: list = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioResult:
    """Complete result of running one scenario."""
    scenario_name: str
    trace_root_id: str
    passed: bool
    violations: list
    phases: Dict[str, PhaseResult]
    event_log: list
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/harness/test_types.py -v`
Expected: All 11 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/types.py tests/unit/harness/__init__.py tests/unit/harness/test_types.py
git commit -m "feat(disease9): add core data types for cross-repo harness (Task 2)"
```

---

## Task 3: StateOracle Protocol + MockStateOracle

**Files:**
- Create: `tests/harness/state_oracle.py`
- Create: `tests/unit/harness/test_state_oracle.py`

**Step 1: Write the failing test**

Create `tests/unit/harness/test_state_oracle.py`:

```python
"""Tests for StateOracle protocol and MockStateOracle."""
import asyncio
import time
import pytest

from tests.harness.types import (
    ComponentStatus, ContractStatus, ContractReasonCode,
    ObservedEvent, OracleObservation,
)


class TestMockStateOracleBasics:
    def test_initial_component_status_unknown(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.UNKNOWN
        assert obs.observation_quality == "fresh"

    def test_set_and_get_component_status(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_component_status("prime", ComponentStatus.READY)
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.READY

    def test_set_component_emits_event(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_phase("setup")
        oracle.set_component_status("prime", ComponentStatus.READY)
        events = oracle.event_log()
        assert len(events) == 1
        assert events[0].event_type == "state_change"
        assert events[0].new_value == "READY"
        assert events[0].scenario_phase == "setup"

    def test_event_seq_monotonic(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_component_status("a", ComponentStatus.READY)
        oracle.set_component_status("b", ComponentStatus.FAILED)
        events = oracle.event_log()
        assert events[0].oracle_event_seq < events[1].oracle_event_seq

    def test_routing_decision(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_routing_decision("CLOUD_CLAUDE")
        obs = oracle.routing_decision()
        assert obs.value == "CLOUD_CLAUDE"

    def test_epoch(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        assert oracle.epoch() == 0
        oracle.set_epoch(3)
        assert oracle.epoch() == 3

    def test_contract_status(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        cs = ContractStatus(compatible=True, reason_code=ContractReasonCode.OK)
        oracle.set_contract_status("prime_handshake", cs)
        result = oracle.contract_status("prime_handshake")
        assert result.compatible is True

    def test_store_revision(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_store_revision("journal", 42)
        assert oracle.store_revision("journal") == 42

    def test_event_log_since_phase(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_phase("setup")
        oracle.set_component_status("a", ComponentStatus.READY)
        oracle.set_phase("inject")
        oracle.set_component_status("b", ComponentStatus.FAILED)
        events = oracle.event_log(since_phase="inject")
        assert len(events) == 1
        assert events[0].component == "b"

    def test_current_seq(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        seq0 = oracle.current_seq()
        oracle.set_component_status("a", ComponentStatus.READY)
        assert oracle.current_seq() > seq0


class TestMockStateOracleWaitUntil:
    @pytest.mark.asyncio
    async def test_wait_until_already_true(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_component_status("prime", ComponentStatus.READY)
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.READY,
            deadline=1.0,
        )

    @pytest.mark.asyncio
    async def test_wait_until_becomes_true(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()

        async def set_ready_later():
            await asyncio.sleep(0.1)
            oracle.set_component_status("prime", ComponentStatus.READY)

        asyncio.get_event_loop().create_task(set_ready_later())
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.READY,
            deadline=2.0,
        )
        assert oracle.component_status("prime").value == ComponentStatus.READY

    @pytest.mark.asyncio
    async def test_wait_until_timeout(self):
        from tests.harness.state_oracle import MockStateOracle, OracleTimeoutError
        oracle = MockStateOracle()
        with pytest.raises(OracleTimeoutError):
            await oracle.wait_until(
                lambda: False,
                deadline=0.3,
                description="never-true predicate",
            )


class TestMockStateOraclePhaseFencing:
    def test_fence_excludes_stale_events(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_phase("setup")
        oracle.set_component_status("a", ComponentStatus.READY)
        setup_seq = oracle.current_seq()

        oracle.set_phase("inject")
        oracle.fence_phase("inject", setup_seq)
        oracle.set_component_status("b", ComponentStatus.FAILED)

        # since_phase="inject" should NOT include setup events
        events = oracle.event_log(since_phase="inject")
        assert all(e.component != "a" for e in events)
        assert any(e.component == "b" for e in events)


class TestMockStateOracleEmitEvent:
    def test_emit_assigns_seq(self):
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_phase("inject")
        oracle.emit_event(
            source="fault_injector",
            event_type="fault_injected",
            component="prime",
            new_value="SIGKILL",
            trace_root_id="r1",
            trace_id="f1",
            metadata={"scope": "process"},
        )
        events = oracle.event_log()
        assert len(events) == 1
        assert events[0].oracle_event_seq > 0
        assert events[0].source == "fault_injector"


class TestStateOracleProtocol:
    def test_mock_satisfies_protocol(self):
        from tests.harness.state_oracle import MockStateOracle, StateOracleProtocol
        oracle = MockStateOracle()
        assert isinstance(oracle, StateOracleProtocol)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/harness/test_state_oracle.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/state_oracle.py`:

```python
"""StateOracle: normalized read interface for cross-repo test harness.

Design doc: docs/plans/2026-03-05-cross-repo-integration-tests-design.md, Section 3.
"""
import asyncio
import itertools
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from tests.harness.types import (
    ComponentStatus, ContractStatus, ContractReasonCode,
    ObservedEvent, OracleObservation,
)


class OracleTimeoutError(Exception):
    """Raised when wait_until exceeds its deadline."""
    pass


class OracleDivergenceError(Exception):
    """Raised when oracle sources disagree in strict mode."""
    pass


@runtime_checkable
class StateOracleProtocol(Protocol):
    """Normalized read-only view of system state for scenario assertions."""

    def component_status(self, name: str) -> OracleObservation: ...
    def routing_decision(self) -> OracleObservation: ...
    def epoch(self) -> int: ...
    def contract_status(self, contract_name: str) -> ContractStatus: ...
    def store_revision(self, store_name: str) -> int: ...
    def event_log(self, since_phase: Optional[str] = None) -> List[ObservedEvent]: ...
    def current_seq(self) -> int: ...
    def current_phase(self) -> str: ...
    async def wait_until(self, predicate: Callable[[], bool], deadline: float,
                         description: str = "") -> None: ...
    def emit_event(self, *, source: str, event_type: str,
                   component: Optional[str] = None,
                   old_value: Optional[str] = None,
                   new_value: str, trace_root_id: str = "",
                   trace_id: str = "",
                   metadata: Optional[Dict[str, Any]] = None) -> int: ...
    def fence_phase(self, phase: str, boundary_seq: int) -> None: ...


class MockStateOracle:
    """In-memory StateOracle for mock-mode scenarios (CI-safe, deterministic)."""

    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self._current_seq = 0
        self._component_statuses: Dict[str, ComponentStatus] = {}
        self._routing: str = "UNKNOWN"
        self._epoch: int = 0
        self._contracts: Dict[str, ContractStatus] = {}
        self._store_revisions: Dict[str, int] = {}
        self._events: List[ObservedEvent] = []
        self._phase: str = "setup"
        self._phase_boundaries: Dict[str, int] = {}
        self._state_change_callbacks: List[Callable] = []

    # ── Mutators (test harness calls these) ──────────────────────

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def set_component_status(self, name: str, status: ComponentStatus) -> None:
        old = self._component_statuses.get(name, ComponentStatus.UNKNOWN)
        self._component_statuses[name] = status
        self._emit(
            source="mock_state_oracle", event_type="state_change",
            component=name, old_value=old.value, new_value=status.value,
        )
        for cb in self._state_change_callbacks:
            cb()

    def set_routing_decision(self, decision: str) -> None:
        old = self._routing
        self._routing = decision
        self._emit(
            source="mock_state_oracle", event_type="routing_change",
            component=None, old_value=old, new_value=decision,
        )

    def set_epoch(self, epoch: int) -> None:
        self._epoch = epoch

    def set_contract_status(self, name: str, status: ContractStatus) -> None:
        self._contracts[name] = status

    def set_store_revision(self, name: str, revision: int) -> None:
        self._store_revisions[name] = revision

    def on_state_change(self, callback: Callable) -> None:
        """Register callback for state changes (used by wait_until)."""
        self._state_change_callbacks.append(callback)

    def fence_phase(self, phase: str, boundary_seq: int) -> None:
        """Record phase boundary seq for event filtering."""
        self._phase_boundaries[phase] = boundary_seq

    # ── StateOracleProtocol implementation ───────────────────────

    def component_status(self, name: str) -> OracleObservation:
        status = self._component_statuses.get(name, ComponentStatus.UNKNOWN)
        return OracleObservation(
            value=status, observed_at_mono=time.monotonic(),
            observation_quality="fresh", source=f"mock:{name}",
        )

    def routing_decision(self) -> OracleObservation:
        return OracleObservation(
            value=self._routing, observed_at_mono=time.monotonic(),
            observation_quality="fresh", source="mock:router",
        )

    def epoch(self) -> int:
        return self._epoch

    def contract_status(self, contract_name: str) -> ContractStatus:
        return self._contracts.get(
            contract_name,
            ContractStatus(compatible=True, reason_code=ContractReasonCode.OK),
        )

    def store_revision(self, store_name: str) -> int:
        return self._store_revisions.get(store_name, 0)

    def event_log(self, since_phase: Optional[str] = None) -> List[ObservedEvent]:
        if since_phase is None:
            return list(self._events)
        boundary_seq = self._phase_boundaries.get(since_phase, 0)
        return [e for e in self._events if e.oracle_event_seq > boundary_seq]

    def current_seq(self) -> int:
        return self._current_seq

    def current_phase(self) -> str:
        return self._phase

    async def wait_until(self, predicate: Callable[[], bool], deadline: float,
                         description: str = "") -> None:
        """Wait until predicate returns True, polling on state changes."""
        if predicate():
            return

        event = asyncio.Event()
        self.on_state_change(lambda: event.set())

        start = time.monotonic()
        while not predicate():
            remaining = deadline - (time.monotonic() - start)
            if remaining <= 0:
                raise OracleTimeoutError(
                    f"wait_until timed out after {deadline}s: {description}"
                )
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.1))
            except asyncio.TimeoutError:
                pass

    def emit_event(self, *, source: str, event_type: str,
                   component: Optional[str] = None,
                   old_value: Optional[str] = None,
                   new_value: str, trace_root_id: str = "",
                   trace_id: str = "",
                   metadata: Optional[Dict[str, Any]] = None) -> int:
        """Emit an event with oracle-assigned seq. External callers use this."""
        return self._emit(
            source=source, event_type=event_type,
            component=component, old_value=old_value, new_value=new_value,
            trace_root_id=trace_root_id, trace_id=trace_id,
            metadata=metadata or {},
        )

    # ── Internal ─────────────────────────────────────────────────

    def _emit(self, *, source: str, event_type: str,
              component: Optional[str], old_value: Optional[str] = None,
              new_value: str, trace_root_id: str = "",
              trace_id: str = "",
              metadata: Optional[Dict[str, Any]] = None) -> int:
        seq = next(self._seq)
        self._current_seq = seq
        event = ObservedEvent(
            oracle_event_seq=seq,
            timestamp_mono=time.monotonic(),
            source=source, event_type=event_type,
            component=component, old_value=old_value, new_value=new_value,
            epoch=self._epoch, scenario_phase=self._phase,
            trace_root_id=trace_root_id, trace_id=trace_id,
            metadata=metadata or {},
        )
        self._events.append(event)
        return seq
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/harness/test_state_oracle.py -v`
Expected: All 16 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/state_oracle.py tests/unit/harness/test_state_oracle.py
git commit -m "feat(disease9): add StateOracle protocol and MockStateOracle (Task 3)"
```

---

## Task 4: InvariantRegistry

**Files:**
- Create: `tests/harness/invariants.py`
- Create: `tests/unit/harness/test_invariants.py`

**Step 1: Write the failing test**

Create `tests/unit/harness/test_invariants.py`:

```python
"""Tests for InvariantRegistry."""
import pytest
from tests.harness.types import ComponentStatus


class TestInvariantRegistry:
    def test_no_invariants_no_violations(self):
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.state_oracle import MockStateOracle
        registry = InvariantRegistry()
        oracle = MockStateOracle()
        assert registry.check_all(oracle) == []

    def test_passing_invariant(self):
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.state_oracle import MockStateOracle
        registry = InvariantRegistry()
        registry.register("always_ok", lambda o: None)
        oracle = MockStateOracle()
        assert registry.check_all(oracle) == []

    def test_failing_invariant(self):
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.state_oracle import MockStateOracle
        registry = InvariantRegistry()
        registry.register("always_fail", lambda o: "something broke")
        oracle = MockStateOracle()
        violations = registry.check_all(oracle)
        assert len(violations) == 1
        assert "[always_fail]" in violations[0]

    def test_flapping_suppression_on(self):
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.state_oracle import MockStateOracle
        registry = InvariantRegistry(debounce_window_s=10.0)
        registry.register("flappy", lambda o: "flap", suppress_flapping=True)
        oracle = MockStateOracle()
        # First check: violation reported
        v1 = registry.check_all(oracle)
        assert len(v1) == 1
        # Second check within debounce window: suppressed
        v2 = registry.check_all(oracle)
        assert len(v2) == 0

    def test_flapping_suppression_off_for_critical(self):
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.state_oracle import MockStateOracle
        registry = InvariantRegistry(debounce_window_s=10.0)
        registry.register("critical", lambda o: "bad", suppress_flapping=False)
        oracle = MockStateOracle()
        v1 = registry.check_all(oracle)
        v2 = registry.check_all(oracle)
        # Both should report (no suppression)
        assert len(v1) == 1
        assert len(v2) == 1

    def test_suppressed_count_tracked(self):
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.state_oracle import MockStateOracle
        registry = InvariantRegistry(debounce_window_s=10.0)
        registry.register("flappy", lambda o: "flap", suppress_flapping=True)
        oracle = MockStateOracle()
        registry.check_all(oracle)
        registry.check_all(oracle)
        registry.check_all(oracle)
        assert registry.suppressed_counts.get("flappy", 0) == 2


class TestMVPInvariants:
    def test_epoch_monotonic_passes(self):
        from tests.harness.invariants import epoch_monotonic
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_epoch(5)
        checker = epoch_monotonic()
        assert checker(oracle) is None

    def test_epoch_monotonic_fails_on_decrease(self):
        from tests.harness.invariants import epoch_monotonic
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_epoch(5)
        checker = epoch_monotonic()
        checker(oracle)  # record epoch=5
        oracle.set_epoch(3)
        result = checker(oracle)
        assert result is not None
        assert "decreased" in result

    def test_single_routing_target_passes(self):
        from tests.harness.invariants import single_routing_target
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        oracle.set_routing_decision("CLOUD_CLAUDE")
        assert single_routing_target()(oracle) is None

    def test_single_routing_target_fails_on_unknown(self):
        from tests.harness.invariants import single_routing_target
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        # Default is "UNKNOWN"
        result = single_routing_target()(oracle)
        assert result is not None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/harness/test_invariants.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/invariants.py`:

```python
"""InvariantRegistry and MVP invariant functions.

Invariants run on two triggers:
1. Event-driven: after every state change
2. Periodic watchdog: every invariant_poll_interval_s (caller responsibility)

Design doc: docs/plans/2026-03-05-cross-repo-integration-tests-design.md, Section 3.6.
"""
import time
from typing import Callable, Dict, List, Optional, Tuple

from tests.harness.types import ComponentStatus


class InvariantRegistry:
    """Pluggable invariant checks with optional flapping suppression."""

    def __init__(self, debounce_window_s: float = 5.0) -> None:
        self._invariants: List[Tuple[str, Callable, bool]] = []
        self._debounce_window_s = debounce_window_s
        self._last_violation: Dict[str, float] = {}
        self.suppressed_counts: Dict[str, int] = {}

    def register(self, name: str, check: Callable, suppress_flapping: bool = True) -> None:
        """Register an invariant check.

        check(oracle) -> None if OK, error string if violated.
        suppress_flapping: if True, suppress repeated violations within debounce window.
        """
        self._invariants.append((name, check, suppress_flapping))

    def check_all(self, oracle) -> List[str]:
        """Evaluate all invariants. Returns list of violation strings."""
        violations = []
        now = time.monotonic()
        for name, check, suppress in self._invariants:
            result = check(oracle)
            if result is not None:
                if suppress and name in self._last_violation:
                    if now - self._last_violation[name] < self._debounce_window_s:
                        self.suppressed_counts[name] = self.suppressed_counts.get(name, 0) + 1
                        continue
                self._last_violation[name] = now
                violations.append(f"[{name}] {result}")
        return violations


# ── MVP Invariant Factories ──────────────────────────────────────────

def epoch_monotonic() -> Callable:
    """Epoch must never decrease."""
    last_epoch = [None]

    def check(oracle) -> Optional[str]:
        current = oracle.epoch()
        if last_epoch[0] is not None and current < last_epoch[0]:
            return f"Epoch decreased from {last_epoch[0]} to {current}"
        last_epoch[0] = current
        return None

    return check


def single_routing_target() -> Callable:
    """Exactly one known routing target must be active."""
    valid_targets = {"LOCAL_PRIME", "GCP_PRIME", "CLOUD_CLAUDE", "HYBRID", "CACHED", "DEGRADED"}

    def check(oracle) -> Optional[str]:
        obs = oracle.routing_decision()
        if obs.value not in valid_targets:
            return f"Routing target '{obs.value}' is not a known target"
        return None

    return check


def fault_isolation(affected: frozenset, unaffected: frozenset) -> Callable:
    """Faults must not leak beyond declared scope."""

    def check(oracle) -> Optional[str]:
        for name in unaffected:
            obs = oracle.component_status(name)
            if obs.value in (ComponentStatus.FAILED, ComponentStatus.LOST):
                return f"Unaffected component {name} is {obs.value.value}"
        return None

    return check


def terminal_is_final() -> Callable:
    """STOPPED/FAILED states never produce non-restart transitions."""
    last_states: Dict[str, str] = {}

    def check(oracle) -> Optional[str]:
        events = oracle.event_log()
        for event in events:
            if event.component is None:
                continue
            if event.old_value in ("STOPPED", "FAILED"):
                if event.new_value not in ("STARTING", "STOPPED", "FAILED"):
                    return (f"{event.component}: illegal transition "
                            f"{event.old_value} -> {event.new_value}")
        return None

    return check
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/harness/test_invariants.py -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/invariants.py tests/unit/harness/test_invariants.py
git commit -m "feat(disease9): add InvariantRegistry with MVP invariants (Task 4)"
```

---

## Task 5: ScopedFaultInjector

**Files:**
- Create: `tests/harness/scoped_fault_injector.py`
- Create: `tests/unit/harness/test_scoped_fault_injector.py`

**Step 1: Write the failing test**

Create `tests/unit/harness/test_scoped_fault_injector.py`:

```python
"""Tests for ScopedFaultInjector."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.harness.types import (
    ComponentStatus, FaultScope, FaultComposition, FaultHandle,
)


class _MockInnerResult:
    def __init__(self):
        self.revert = AsyncMock()


def _make_oracle():
    from tests.harness.state_oracle import MockStateOracle
    oracle = MockStateOracle()
    oracle.set_phase("inject")
    oracle.set_component_status("prime", ComponentStatus.READY)
    oracle.set_component_status("backend", ComponentStatus.READY)
    oracle.set_component_status("trinity", ComponentStatus.READY)
    return oracle


def _make_inner():
    inner = MagicMock()
    result = _MockInnerResult()
    inner.inject_failure = AsyncMock(return_value=result)
    return inner, result


class TestScopedFaultInjectorBasics:
    @pytest.mark.asyncio
    async def test_inject_returns_handle(self):
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        oracle = _make_oracle()
        inner, _ = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        handle = await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
        )
        assert isinstance(handle, FaultHandle)
        assert handle.target == "prime"
        assert handle.pre_fault_baseline == {"prime": "READY"}

    @pytest.mark.asyncio
    async def test_inject_emits_event(self):
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        oracle = _make_oracle()
        inner, _ = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        await injector.inject(
            scope=FaultScope.PROCESS, target="prime",
            fault_type="sigkill",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
        )
        events = oracle.event_log()
        fault_events = [e for e in events if e.event_type == "fault_injected"]
        assert len(fault_events) >= 1
        assert fault_events[-1].new_value == "sigkill"

    @pytest.mark.asyncio
    async def test_inject_delegates_to_inner(self):
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        oracle = _make_oracle()
        inner, _ = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
        )
        inner.inject_failure.assert_awaited_once_with("prime", "crash")


class TestScopedFaultInjectorReentrant:
    @pytest.mark.asyncio
    async def test_reject_duplicate_fault(self):
        from tests.harness.scoped_fault_injector import (
            ScopedFaultInjector, ReentrantFaultError,
        )
        oracle = _make_oracle()
        inner, _ = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
        )
        with pytest.raises(ReentrantFaultError):
            await injector.inject(
                scope=FaultScope.TRANSPORT, target="prime",
                fault_type="partition",
                affected=frozenset({"prime"}),
                unaffected=frozenset({"backend"}),
            )

    @pytest.mark.asyncio
    async def test_replace_reverts_existing(self):
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        oracle = _make_oracle()
        inner, result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        handle1 = await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
        )
        # Replace should revert handle1 first
        handle2 = await injector.inject(
            scope=FaultScope.TRANSPORT, target="prime",
            fault_type="partition",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
            composition=FaultComposition.REPLACE,
        )
        assert handle2.fault_id != handle1.fault_id


class TestScopedFaultInjectorRevert:
    @pytest.mark.asyncio
    async def test_revert_calls_inner_revert(self):
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        oracle = _make_oracle()
        inner, result = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        handle = await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend", "trinity"}),
            convergence_deadline_s=0.5,
        )
        # Set prime back to READY so convergence passes
        oracle.set_component_status("prime", ComponentStatus.READY)
        await injector.revert(handle)
        result.revert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_revert_clears_active(self):
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        oracle = _make_oracle()
        inner, _ = _make_inner()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        handle = await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
            convergence_deadline_s=0.5,
        )
        oracle.set_component_status("prime", ComponentStatus.READY)
        await injector.revert(handle)
        # Should be able to inject again (not blocked)
        handle2 = await injector.inject(
            scope=FaultScope.COMPONENT, target="prime",
            fault_type="crash2",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
        )
        assert handle2.fault_id != handle.fault_id
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/harness/test_scoped_fault_injector.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/scoped_fault_injector.py`:

```python
"""ScopedFaultInjector: wraps existing FaultInjector with scope boundaries.

Design doc: docs/plans/2026-03-05-cross-repo-integration-tests-design.md, Section 3.5.
"""
import time
from typing import Any, Dict, FrozenSet, Optional
from uuid import uuid4

from tests.harness.types import (
    ComponentStatus, FaultComposition, FaultHandle, FaultScope,
)


class ReentrantFaultError(Exception):
    """Raised when injecting overlapping fault on same target without composition policy."""
    pass


class FaultIsolationError(Exception):
    """Raised when fault leaks beyond declared scope."""
    pass


class ScopedFaultInjector:
    """Wraps existing FaultInjector with scope tracking and event provenance."""

    def __init__(self, inner: Any, oracle: Any) -> None:
        self._inner = inner
        self._oracle = oracle
        self._active_by_target: Dict[str, FaultHandle] = {}

    async def inject(
        self,
        *,
        scope: FaultScope,
        target: str,
        fault_type: str,
        affected: FrozenSet[str],
        unaffected: FrozenSet[str],
        composition: FaultComposition = FaultComposition.REJECT,
        convergence_deadline_s: float = 30.0,
        trace_root_id: str = "",
        **kwargs: Any,
    ) -> FaultHandle:
        """Inject a scoped fault with provenance tracking."""
        # Re-entrant guard
        if target in self._active_by_target:
            if composition == FaultComposition.REJECT:
                raise ReentrantFaultError(
                    f"Fault already active on {target}. "
                    f"Use composition=REPLACE or STACK to override."
                )
            elif composition == FaultComposition.REPLACE:
                existing = self._active_by_target[target]
                await existing.revert()
                del self._active_by_target[target]

        # Capture pre-fault baseline
        baseline = {}
        for name in affected:
            obs = self._oracle.component_status(name)
            baseline[name] = obs.value.value if hasattr(obs.value, 'value') else str(obs.value)

        # Delegate to inner injector
        inner_result = await self._inner.inject_failure(target, fault_type)

        fault_id = uuid4().hex[:12]

        handle = FaultHandle(
            fault_id=fault_id,
            scope=scope,
            target=target,
            affected_components=affected,
            unaffected_components=unaffected,
            pre_fault_baseline=baseline,
            convergence_deadline_s=convergence_deadline_s,
            revert=inner_result.revert,
        )

        # Emit provenance event via oracle (oracle assigns seq)
        self._oracle.emit_event(
            source="fault_injector",
            event_type="fault_injected",
            component=target,
            new_value=fault_type,
            trace_root_id=trace_root_id,
            trace_id=fault_id,
            metadata={"scope": scope.value, "affected": list(affected)},
        )

        self._active_by_target[target] = handle
        return handle

    async def revert(self, handle: FaultHandle) -> None:
        """Revert a fault and verify isolation + convergence."""
        await handle.revert()

        # Check isolation: unaffected components must not be FAILED/LOST
        for name in handle.unaffected_components:
            obs = self._oracle.component_status(name)
            status = obs.value
            if status in (ComponentStatus.FAILED, ComponentStatus.LOST):
                raise FaultIsolationError(
                    f"Fault {handle.fault_id} (scope={handle.scope.value}, "
                    f"target={handle.target}) leaked to unaffected "
                    f"component {name} (status={status.value})"
                )

        # Convergence: wait for affected components to recover
        await self._oracle.wait_until(
            lambda: all(
                self._oracle.component_status(c).value in (
                    ComponentStatus.READY, ComponentStatus.DEGRADED,
                )
                for c in handle.affected_components
            ),
            deadline=handle.convergence_deadline_s,
            description=f"post-revert convergence for {handle.fault_id}",
        )

        # Clear active tracking
        if handle.target in self._active_by_target:
            del self._active_by_target[handle.target]

    @property
    def active_faults(self) -> Dict[str, FaultHandle]:
        return dict(self._active_by_target)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/harness/test_scoped_fault_injector.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/scoped_fault_injector.py tests/unit/harness/test_scoped_fault_injector.py
git commit -m "feat(disease9): add ScopedFaultInjector with scope boundaries (Task 5)"
```

---

## Task 6: HarnessOrchestrator

**Files:**
- Create: `tests/harness/orchestrator.py`
- Create: `tests/unit/harness/test_orchestrator.py`

**Step 1: Write the failing test**

Create `tests/unit/harness/test_orchestrator.py`:

```python
"""Tests for HarnessOrchestrator."""
import asyncio
import pytest
from dataclasses import dataclass
from typing import Dict, Optional

from tests.harness.types import ComponentStatus, PhaseFailure


@dataclass
class HarnessConfig:
    strict_mode: bool = False


class DummyScenario:
    """Scenario that tracks which phases ran."""
    name = "dummy"
    phase_deadlines: Dict[str, float] = {"setup": 5, "inject": 5, "verify": 5, "recover": 5}

    def __init__(self):
        self.phases_run = []

    async def setup(self, oracle, injector, trace_root_id):
        self.phases_run.append("setup")

    async def inject(self, oracle, injector, trace_root_id):
        self.phases_run.append("inject")

    async def verify(self, oracle, injector, trace_root_id):
        self.phases_run.append("verify")

    async def recover(self, oracle, injector, trace_root_id):
        self.phases_run.append("recover")


class TimeoutScenario:
    """Scenario where inject phase times out."""
    name = "timeout_test"
    phase_deadlines = {"setup": 5, "inject": 0.2, "verify": 5, "recover": 5}

    async def setup(self, oracle, injector, trace_root_id):
        pass

    async def inject(self, oracle, injector, trace_root_id):
        await asyncio.sleep(10)  # will timeout

    async def verify(self, oracle, injector, trace_root_id):
        pass

    async def recover(self, oracle, injector, trace_root_id):
        pass


class InvariantFailScenario:
    """Scenario where an invariant fails during verify."""
    name = "invariant_fail"
    phase_deadlines = {"setup": 5, "inject": 5, "verify": 5, "recover": 5}

    async def setup(self, oracle, injector, trace_root_id):
        pass

    async def inject(self, oracle, injector, trace_root_id):
        # Break the epoch
        oracle.set_epoch(5)

    async def verify(self, oracle, injector, trace_root_id):
        oracle.set_epoch(2)  # decrease epoch — invariant violation

    async def recover(self, oracle, injector, trace_root_id):
        pass


class TestOrchestratorBasics:
    @pytest.mark.asyncio
    async def test_runs_all_four_phases(self):
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from unittest.mock import MagicMock

        oracle = MockStateOracle()
        injector = ScopedFaultInjector(inner=MagicMock(), oracle=oracle)
        invariants = InvariantRegistry()
        config = HarnessConfig()

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=config,
        )
        scenario = DummyScenario()
        result = await orch.run_scenario(scenario)

        assert result.passed is True
        assert scenario.phases_run == ["setup", "inject", "verify", "recover"]
        assert result.scenario_name == "dummy"
        assert len(result.trace_root_id) > 0

    @pytest.mark.asyncio
    async def test_phase_timeout_stops_execution(self):
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from unittest.mock import MagicMock

        oracle = MockStateOracle()
        injector = ScopedFaultInjector(inner=MagicMock(), oracle=oracle)
        invariants = InvariantRegistry()
        config = HarnessConfig()

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=config,
        )
        scenario = TimeoutScenario()
        result = await orch.run_scenario(scenario)

        assert result.passed is False
        timeout_violations = [v for v in result.violations
                              if v.failure_type == "phase_timeout"]
        assert len(timeout_violations) == 1
        assert timeout_violations[0].phase == "inject"

    @pytest.mark.asyncio
    async def test_invariant_violation_recorded(self):
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry, epoch_monotonic
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from unittest.mock import MagicMock

        oracle = MockStateOracle()
        injector = ScopedFaultInjector(inner=MagicMock(), oracle=oracle)
        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        config = HarnessConfig()

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=config,
        )
        scenario = InvariantFailScenario()
        result = await orch.run_scenario(scenario)

        assert result.passed is False
        inv_violations = [v for v in result.violations
                          if v.failure_type == "invariant_violation"]
        assert len(inv_violations) >= 1

    @pytest.mark.asyncio
    async def test_phase_boundary_seq_recorded(self):
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from unittest.mock import MagicMock

        oracle = MockStateOracle()
        injector = ScopedFaultInjector(inner=MagicMock(), oracle=oracle)
        invariants = InvariantRegistry()
        config = HarnessConfig()

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=config,
        )
        scenario = DummyScenario()
        await orch.run_scenario(scenario)

        # Oracle should have phase boundaries recorded
        assert "setup" in oracle._phase_boundaries or "inject" in oracle._phase_boundaries

    @pytest.mark.asyncio
    async def test_result_includes_event_log(self):
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from unittest.mock import MagicMock

        oracle = MockStateOracle()
        oracle.set_component_status("prime", ComponentStatus.READY)
        injector = ScopedFaultInjector(inner=MagicMock(), oracle=oracle)
        invariants = InvariantRegistry()
        config = HarnessConfig()

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=config,
        )
        scenario = DummyScenario()
        result = await orch.run_scenario(scenario)
        assert len(result.event_log) >= 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/harness/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/orchestrator.py`:

```python
"""HarnessOrchestrator: runs scenarios through four-phase lifecycle.

Design doc: docs/plans/2026-03-05-cross-repo-integration-tests-design.md, Section 4.1.
"""
import asyncio
import time
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from tests.harness.types import PhaseFailure, PhaseResult, ScenarioResult


class HarnessOrchestrator:
    """Executes scenarios with invariant checking and phase fencing."""

    def __init__(
        self,
        mode: Literal["mock", "real"],
        oracle: Any,
        injector: Any,
        invariants: Any,
        config: Any,
    ) -> None:
        self._mode = mode
        self._oracle = oracle
        self._injector = injector
        self._invariants = invariants
        self._config = config

    async def run_scenario(self, scenario: Any) -> ScenarioResult:
        """Run a scenario through setup -> inject -> verify -> recover."""
        trace_root_id = uuid4().hex[:16]
        violations: List[PhaseFailure] = []
        phase_results: Dict[str, PhaseResult] = {}

        for phase_name in ("setup", "inject", "verify", "recover"):
            self._oracle.set_phase(phase_name)
            boundary_seq = self._oracle.current_seq()

            phase_fn = getattr(scenario, phase_name)
            phase_start = time.monotonic()
            phase_violations: List[PhaseFailure] = []

            try:
                deadline = scenario.phase_deadlines.get(phase_name, 60.0)
                await asyncio.wait_for(
                    phase_fn(self._oracle, self._injector, trace_root_id),
                    timeout=deadline,
                )
            except asyncio.TimeoutError:
                failure = PhaseFailure(
                    phase=phase_name,
                    failure_type="phase_timeout",
                    detail=f"Exceeded {scenario.phase_deadlines.get(phase_name, 60.0)}s deadline",
                )
                violations.append(failure)
                phase_violations.append(failure)
                phase_results[phase_name] = PhaseResult(
                    duration_s=time.monotonic() - phase_start,
                    violations=phase_violations,
                )
                break

            # Invariant check on phase boundary
            inv_violations = self._invariants.check_all(self._oracle)
            for v in inv_violations:
                failure = PhaseFailure(
                    phase=phase_name,
                    failure_type="invariant_violation",
                    detail=v,
                )
                violations.append(failure)
                phase_violations.append(failure)

            # Phase boundary fence
            self._oracle.fence_phase(phase_name, boundary_seq)

            phase_results[phase_name] = PhaseResult(
                duration_s=time.monotonic() - phase_start,
                violations=phase_violations,
            )

        return ScenarioResult(
            scenario_name=scenario.name,
            trace_root_id=trace_root_id,
            passed=len(violations) == 0,
            violations=violations,
            phases=phase_results,
            event_log=self._oracle.event_log(),
        )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/harness/test_orchestrator.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/orchestrator.py tests/unit/harness/test_orchestrator.py
git commit -m "feat(disease9): add HarnessOrchestrator with phase fencing (Task 6)"
```

---

## Task 7: MockComponentProcess

**Files:**
- Create: `tests/harness/component_process.py`
- Create: `tests/unit/harness/test_component_process.py`

**Step 1: Write the failing test**

Create `tests/unit/harness/test_component_process.py`:

```python
"""Tests for MockComponentProcess."""
import pytest
from tests.harness.types import ComponentStatus


class TestMockComponentProcess:
    @pytest.mark.asyncio
    async def test_start_transitions_to_ready(self):
        from tests.harness.component_process import MockComponentProcess
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)
        await proc.start()
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.READY

    @pytest.mark.asyncio
    async def test_stop_transitions_to_stopped(self):
        from tests.harness.component_process import MockComponentProcess
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)
        await proc.start()
        await proc.stop()
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.STOPPED

    @pytest.mark.asyncio
    async def test_kill_transitions_to_failed(self):
        from tests.harness.component_process import MockComponentProcess
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)
        await proc.start()
        await proc.kill()
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.FAILED

    @pytest.mark.asyncio
    async def test_start_after_kill_recovers(self):
        from tests.harness.component_process import MockComponentProcess
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)
        await proc.start()
        await proc.kill()
        await proc.start()
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.READY

    def test_initial_status_registered(self):
        from tests.harness.component_process import MockComponentProcess
        from tests.harness.state_oracle import MockStateOracle
        oracle = MockStateOracle()
        proc = MockComponentProcess(name="prime", oracle=oracle)
        obs = oracle.component_status("prime")
        assert obs.value == ComponentStatus.UNKNOWN  # not yet started
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/harness/test_component_process.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/component_process.py`:

```python
"""ComponentProcess: abstract base and mock implementation.

Design doc: docs/plans/2026-03-05-cross-repo-integration-tests-design.md, Section 1.1.
"""
from abc import ABC, abstractmethod
from typing import Any

from tests.harness.types import ComponentStatus


class ComponentProcess(ABC):
    """Abstract base for component lifecycle management."""

    def __init__(self, name: str, oracle: Any) -> None:
        self.name = name
        self._oracle = oracle

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def kill(self) -> None: ...


class MockComponentProcess(ComponentProcess):
    """In-memory component for mock-mode scenarios."""

    async def start(self) -> None:
        self._oracle.set_component_status(self.name, ComponentStatus.STARTING)
        self._oracle.set_component_status(self.name, ComponentStatus.HANDSHAKING)
        self._oracle.set_component_status(self.name, ComponentStatus.READY)

    async def stop(self) -> None:
        self._oracle.set_component_status(self.name, ComponentStatus.DRAINING)
        self._oracle.set_component_status(self.name, ComponentStatus.STOPPING)
        self._oracle.set_component_status(self.name, ComponentStatus.STOPPED)

    async def kill(self) -> None:
        self._oracle.set_component_status(self.name, ComponentStatus.FAILED)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/harness/test_component_process.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/component_process.py tests/unit/harness/test_component_process.py
git commit -m "feat(disease9): add ComponentProcess ABC and MockComponentProcess (Task 7)"
```

---

## Task 8: Scenario S1 — Prime Crash -> Fallback to Claude

**Files:**
- Create: `tests/harness/scenarios/__init__.py`
- Create: `tests/harness/scenarios/s01_prime_crash_fallback.py`
- Create: `tests/integration/cross_repo/test_s01_prime_crash_fallback.py`

**Step 1: Write the failing test**

Create `tests/harness/scenarios/__init__.py` (empty).

Create `tests/integration/cross_repo/test_s01_prime_crash_fallback.py`:

```python
"""S1: Prime Crash -> Fallback to Claude.

Verifies: Router converges to CLOUD_CLAUDE within 10s after Prime SIGKILL.
Recovery: Router returns to LOCAL_PRIME within 30s after Prime restart.
"""
import pytest
from tests.harness.types import ComponentStatus, FaultScope


@pytest.mark.integration_mock
class TestS01PrimeCrashFallback:
    @pytest.mark.asyncio
    async def test_scenario_passes(self):
        from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry, epoch_monotonic, single_routing_target
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.component_process import MockComponentProcess
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner_result = MagicMock()
        inner_result.revert = AsyncMock()
        inner.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        invariants.register("single_routing_target", single_routing_target(), suppress_flapping=False)

        prime_proc = MockComponentProcess(name="prime", oracle=oracle)
        scenario = S01PrimeCrashFallback(prime_process=prime_proc, oracle=oracle)

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )

        result = await orch.run_scenario(scenario)
        assert result.passed, f"S1 failed: {result.violations}"

    @pytest.mark.asyncio
    async def test_causality_chain(self):
        """Fault event precedes routing change in oracle_event_seq."""
        from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.component_process import MockComponentProcess
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner_result = MagicMock()
        inner_result.revert = AsyncMock()
        inner.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()
        prime_proc = MockComponentProcess(name="prime", oracle=oracle)
        scenario = S01PrimeCrashFallback(prime_process=prime_proc, oracle=oracle)

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )
        result = await orch.run_scenario(scenario)

        # Verify causality: fault_injected comes before routing_change to CLOUD_CLAUDE
        fault_events = [e for e in result.event_log if e.event_type == "fault_injected"]
        route_events = [e for e in result.event_log
                        if e.event_type == "routing_change" and e.new_value == "CLOUD_CLAUDE"]
        assert len(fault_events) >= 1
        assert len(route_events) >= 1
        assert fault_events[0].oracle_event_seq < route_events[0].oracle_event_seq
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/cross_repo/test_s01_prime_crash_fallback.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/scenarios/s01_prime_crash_fallback.py`:

```python
"""S1: Prime Crash -> Fallback to Claude.

Setup: Prime READY, routing to LOCAL_PRIME.
Inject: Kill Prime process.
Verify: Router converges to CLOUD_CLAUDE. Causality: fault precedes route change.
Recover: Restart Prime. Router converges back to LOCAL_PRIME. Epoch increments.

SLO: Fallback <10s, recovery <30s.
"""
from typing import Any, Dict

from tests.harness.types import ComponentStatus, FaultScope


class S01PrimeCrashFallback:
    name = "s01_prime_crash_fallback"
    phase_deadlines: Dict[str, float] = {
        "setup": 5.0, "inject": 10.0, "verify": 10.0, "recover": 30.0,
    }

    def __init__(self, prime_process: Any, oracle: Any) -> None:
        self._prime = prime_process
        self._oracle = oracle
        self._fault_handle = None

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Start prime and set routing
        await self._prime.start()
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Kill prime
        await self._prime.kill()

        # Inject fault via scoped injector for provenance tracking
        self._fault_handle = await injector.inject(
            scope=FaultScope.PROCESS,
            target="prime",
            fault_type="sigkill",
            affected=frozenset({"prime"}),
            unaffected=frozenset(),
            trace_root_id=trace_root_id,
            convergence_deadline_s=30.0,
        )

        # Simulate router detecting failure and switching
        oracle.set_routing_decision("CLOUD_CLAUDE")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Verify routing converged to fallback
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=10.0,
            description="Router converges to CLOUD_CLAUDE",
        )

        # Verify prime is FAILED
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.FAILED,
            deadline=5.0,
            description="Prime is FAILED",
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Restart prime
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(oracle.epoch() + 1)

        # Verify recovery
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=30.0,
            description="Router converges back to LOCAL_PRIME",
        )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/cross_repo/test_s01_prime_crash_fallback.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/scenarios/__init__.py tests/harness/scenarios/s01_prime_crash_fallback.py tests/integration/cross_repo/test_s01_prime_crash_fallback.py
git commit -m "feat(disease9): add S1 Prime Crash Fallback scenario (Task 8)"
```

---

## Task 9: Scenario S5 — Cascading Failure (Hard Dep Propagation)

**Files:**
- Create: `tests/harness/scenarios/s05_cascading_failure.py`
- Create: `tests/integration/cross_repo/test_s05_cascading_failure.py`

**Step 1: Write the failing test**

Create `tests/integration/cross_repo/test_s05_cascading_failure.py`:

```python
"""S5: Cascading Failure -> Hard Dep Propagation.

Verifies: Hard dependents fail, soft dependents degrade, unrelated unaffected.
"""
import pytest
from tests.harness.types import ComponentStatus, FaultScope


@pytest.mark.integration_mock
class TestS05CascadingFailure:
    @pytest.mark.asyncio
    async def test_scenario_passes(self):
        from tests.harness.scenarios.s05_cascading_failure import S05CascadingFailure
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry, fault_isolation
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.component_process import MockComponentProcess
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner_result = MagicMock()
        inner_result.revert = AsyncMock()
        inner.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()
        invariants.register(
            "fault_isolation",
            fault_isolation(
                affected=frozenset({"db", "api", "cache"}),
                unaffected=frozenset({"frontend"}),
            ),
            suppress_flapping=False,
        )

        scenario = S05CascadingFailure(oracle=oracle)

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )
        result = await orch.run_scenario(scenario)
        assert result.passed, f"S5 failed: {result.violations}"

    @pytest.mark.asyncio
    async def test_hard_dep_fails_soft_dep_degrades(self):
        from tests.harness.scenarios.s05_cascading_failure import S05CascadingFailure
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner_result = MagicMock()
        inner_result.revert = AsyncMock()
        inner.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()
        scenario = S05CascadingFailure(oracle=oracle)

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )
        result = await orch.run_scenario(scenario)

        # After inject phase, check the event log for cascade
        inject_events = [e for e in result.event_log
                         if e.scenario_phase == "inject" and e.event_type == "state_change"]
        # api (hard dep) should go to FAILED
        api_failed = [e for e in inject_events
                      if e.component == "api" and e.new_value == "FAILED"]
        assert len(api_failed) >= 1

        # cache (soft dep) should go to DEGRADED
        cache_degraded = [e for e in inject_events
                          if e.component == "cache" and e.new_value == "DEGRADED"]
        assert len(cache_degraded) >= 1

        # frontend (unrelated) should NOT change during inject
        frontend_changes = [e for e in inject_events if e.component == "frontend"]
        assert len(frontend_changes) == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/cross_repo/test_s05_cascading_failure.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/scenarios/s05_cascading_failure.py`:

```python
"""S5: Cascading Failure -> Hard Dep Propagation.

Setup: 4 components: db (root), api (hard dep on db), cache (soft dep on db), frontend (unrelated).
Inject: Fail db.
Verify: api -> FAILED, cache -> DEGRADED, frontend -> unchanged.
Recover: Restart db. api and cache recover.

SLO: Cascade <10s, recovery <60s.
"""
from typing import Any, Dict

from tests.harness.types import ComponentStatus, FaultScope


class S05CascadingFailure:
    name = "s05_cascading_failure"
    phase_deadlines: Dict[str, float] = {
        "setup": 5.0, "inject": 10.0, "verify": 10.0, "recover": 60.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # All four components start READY
        for name in ("db", "api", "cache", "frontend"):
            oracle.set_component_status(name, ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Fail the root component
        await injector.inject(
            scope=FaultScope.COMPONENT,
            target="db",
            fault_type="crash",
            affected=frozenset({"db", "api", "cache"}),
            unaffected=frozenset({"frontend"}),
            trace_root_id=trace_root_id,
            convergence_deadline_s=60.0,
        )
        oracle.set_component_status("db", ComponentStatus.FAILED)

        # Cascade: hard dep fails, soft dep degrades
        oracle.set_component_status("api", ComponentStatus.FAILED)
        oracle.set_component_status("cache", ComponentStatus.DEGRADED)
        # frontend unchanged

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        await oracle.wait_until(
            lambda: oracle.component_status("api").value == ComponentStatus.FAILED,
            deadline=10.0,
            description="api hard dep cascaded to FAILED",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("cache").value == ComponentStatus.DEGRADED,
            deadline=10.0,
            description="cache soft dep degraded",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("frontend").value == ComponentStatus.READY,
            deadline=5.0,
            description="frontend unaffected",
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Restart root
        oracle.set_component_status("db", ComponentStatus.READY)
        # Dependents recover
        oracle.set_component_status("api", ComponentStatus.READY)
        oracle.set_component_status("cache", ComponentStatus.READY)

        await oracle.wait_until(
            lambda: all(
                oracle.component_status(c).value == ComponentStatus.READY
                for c in ("db", "api", "cache", "frontend")
            ),
            deadline=60.0,
            description="All components recovered",
        )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/cross_repo/test_s05_cascading_failure.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/scenarios/s05_cascading_failure.py tests/integration/cross_repo/test_s05_cascading_failure.py
git commit -m "feat(disease9): add S5 Cascading Failure scenario (Task 9)"
```

---

## Task 10: Scenario S7 — Epoch Stale During Transition

**Files:**
- Create: `tests/harness/scenarios/s07_epoch_stale.py`
- Create: `tests/integration/cross_repo/test_s07_epoch_stale.py`

**Step 1: Write the failing test**

Create `tests/integration/cross_repo/test_s07_epoch_stale.py`:

```python
"""S7: Epoch Stale During Transition.

Verifies: Stale-epoch journal write is rejected. Current-epoch state unaffected.
"""
import pytest
from tests.harness.types import ComponentStatus


@pytest.mark.integration_mock
class TestS07EpochStale:
    @pytest.mark.asyncio
    async def test_scenario_passes(self):
        from tests.harness.scenarios.s07_epoch_stale import S07EpochStale
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry, epoch_monotonic
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner.inject_failure = AsyncMock()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)

        scenario = S07EpochStale(oracle=oracle)
        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )
        result = await orch.run_scenario(scenario)
        assert result.passed, f"S7 failed: {result.violations}"

    @pytest.mark.asyncio
    async def test_stale_write_rejected(self):
        """Verify the event log shows stale_epoch_rejected event."""
        from tests.harness.scenarios.s07_epoch_stale import S07EpochStale
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner.inject_failure = AsyncMock()
        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()

        scenario = S07EpochStale(oracle=oracle)
        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )
        result = await orch.run_scenario(scenario)

        stale_events = [e for e in result.event_log
                        if e.event_type == "stale_epoch_rejected"]
        assert len(stale_events) >= 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/cross_repo/test_s07_epoch_stale.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `tests/harness/scenarios/s07_epoch_stale.py`:

```python
"""S7: Epoch Stale During Transition.

Setup: System at epoch 3, Prime READY.
Inject: Simulate stale-epoch write attempt (epoch 2 during epoch 3).
Verify: Write rejected, current state unaffected, epoch monotonicity holds.
Recover: System continues normally.

SLO: Rejection <1s.
"""
from typing import Any, Dict

from tests.harness.types import ComponentStatus


class S07EpochStale:
    name = "s07_epoch_stale"
    phase_deadlines: Dict[str, float] = {
        "setup": 5.0, "inject": 5.0, "verify": 5.0, "recover": 5.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        oracle.set_epoch(3)
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_store_revision("journal", 100)
        oracle.set_routing_decision("LOCAL_PRIME")

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Simulate stale-epoch write attempt
        stale_epoch = oracle.epoch() - 1
        current_epoch = oracle.epoch()

        # The write is rejected — emit rejection event
        oracle.emit_event(
            source="orchestration_journal",
            event_type="stale_epoch_rejected",
            component="prime",
            old_value=str(stale_epoch),
            new_value=str(current_epoch),
            trace_root_id=trace_root_id,
            trace_id="stale_write_attempt",
            metadata={"stale_epoch": stale_epoch, "current_epoch": current_epoch},
        )
        # State is NOT mutated — prime stays READY, epoch stays 3

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # Epoch unchanged
        assert oracle.epoch() == 3

        # Prime still READY
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.READY,
            deadline=1.0,
            description="Prime still READY after stale write rejection",
        )

        # Store revision unchanged
        assert oracle.store_revision("journal") == 100

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        # System continues normally — no action needed
        pass
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/cross_repo/test_s07_epoch_stale.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add tests/harness/scenarios/s07_epoch_stale.py tests/integration/cross_repo/test_s07_epoch_stale.py
git commit -m "feat(disease9): add S7 Epoch Stale scenario (Task 10)"
```

---

## Task 11: Contract Gate Test (Harness MVP Verification)

**Files:**
- Create: `tests/contracts/test_cross_repo_harness_gate.py`

**Step 1: Write the failing test**

Create `tests/contracts/test_cross_repo_harness_gate.py`:

```python
"""Gate test: Disease 9 cross-repo harness MVP verification.

Verifies all Go/No-Go criteria from the design doc.
"""
import pytest


class TestHarnessGate:
    def test_core_types_importable(self):
        from tests.harness.types import (
            ObservedEvent, OracleObservation, FaultScope,
            FaultComposition, ContractStatus, ContractReasonCode,
            ComponentStatus, FaultHandle, PhaseFailure,
            PhaseResult, ScenarioResult,
        )
        assert len(ComponentStatus) == 11
        assert len(FaultScope) == 5
        assert len(ContractReasonCode) == 6

    def test_state_oracle_protocol_exists(self):
        from tests.harness.state_oracle import StateOracleProtocol, MockStateOracle
        oracle = MockStateOracle()
        assert isinstance(oracle, StateOracleProtocol)

    def test_invariant_registry_has_mvp_invariants(self):
        from tests.harness.invariants import (
            InvariantRegistry, epoch_monotonic,
            single_routing_target, fault_isolation, terminal_is_final,
        )
        registry = InvariantRegistry()
        registry.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        registry.register("single_routing_target", single_routing_target(), suppress_flapping=False)
        registry.register("fault_isolation",
                          fault_isolation(frozenset(), frozenset()),
                          suppress_flapping=False)
        registry.register("terminal_is_final", terminal_is_final(), suppress_flapping=False)
        assert len(registry._invariants) == 4

    def test_scoped_fault_injector_importable(self):
        from tests.harness.scoped_fault_injector import (
            ScopedFaultInjector, ReentrantFaultError, FaultIsolationError,
        )

    def test_orchestrator_importable(self):
        from tests.harness.orchestrator import HarnessOrchestrator

    def test_component_process_importable(self):
        from tests.harness.component_process import (
            ComponentProcess, MockComponentProcess,
        )

    def test_scenario_s01_importable(self):
        from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback

    def test_scenario_s05_importable(self):
        from tests.harness.scenarios.s05_cascading_failure import S05CascadingFailure

    def test_scenario_s07_importable(self):
        from tests.harness.scenarios.s07_epoch_stale import S07EpochStale

    @pytest.mark.asyncio
    async def test_full_scenario_s01_mock_mode(self):
        """End-to-end: S1 passes in mock mode."""
        from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback
        from tests.harness.state_oracle import MockStateOracle
        from tests.harness.invariants import InvariantRegistry, epoch_monotonic, single_routing_target
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.component_process import MockComponentProcess
        from unittest.mock import MagicMock, AsyncMock
        from dataclasses import dataclass

        @dataclass
        class Config:
            strict_mode: bool = False

        oracle = MockStateOracle()
        inner = MagicMock()
        inner_result = MagicMock()
        inner_result.revert = AsyncMock()
        inner.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner, oracle=oracle)
        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        invariants.register("single_routing_target", single_routing_target(), suppress_flapping=False)

        prime_proc = MockComponentProcess(name="prime", oracle=oracle)
        scenario = S01PrimeCrashFallback(prime_process=prime_proc, oracle=oracle)

        orch = HarnessOrchestrator(
            mode="mock", oracle=oracle, injector=injector,
            invariants=invariants, config=Config(),
        )
        result = await orch.run_scenario(scenario)
        assert result.passed
        assert len(result.event_log) > 0
        assert result.trace_root_id
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/contracts/test_cross_repo_harness_gate.py -v`
Expected: FAIL (modules not yet created in previous tasks)

**Step 3: Implementation**

No new implementation — this task is the gate test that validates all prior tasks. If Tasks 1-10 are complete, this should pass.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/contracts/test_cross_repo_harness_gate.py -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
git add tests/contracts/test_cross_repo_harness_gate.py
git commit -m "test(disease9): add harness MVP gate test (Task 11)"
```

---

## Task 12: Run Full Test Suite + Verify No Regressions

**Files:**
- No new files

**Step 1: Run all harness unit tests**

Run: `python3 -m pytest tests/unit/harness/ -v`
Expected: All tests PASS (types, state_oracle, invariants, scoped_fault_injector, orchestrator, component_process)

**Step 2: Run all integration scenarios**

Run: `python3 -m pytest tests/integration/cross_repo/ -v -m integration_mock`
Expected: All scenario tests PASS (S1, S5, S7)

**Step 3: Run gate test**

Run: `python3 -m pytest tests/contracts/test_cross_repo_harness_gate.py -v`
Expected: All 10 gate assertions PASS

**Step 4: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/unit/ tests/contracts/ -v --timeout=120`
Expected: No regressions — all existing tests still pass

**Step 5: Final commit (if any fixes needed)**

```bash
git commit -m "fix(disease9): test suite regression fixes (Task 12)"
```

---

## Summary

| Task | Component | Tests |
|---|---|---|
| 1 | Pytest markers + scaffolding | Marker discovery |
| 2 | Core data types (`types.py`) | 11 tests |
| 3 | StateOracle protocol + MockStateOracle | 16 tests |
| 4 | InvariantRegistry + MVP invariants | 10 tests |
| 5 | ScopedFaultInjector | 7 tests |
| 6 | HarnessOrchestrator | 5 tests |
| 7 | MockComponentProcess | 5 tests |
| 8 | Scenario S1 (Prime Crash Fallback) | 2 tests |
| 9 | Scenario S5 (Cascading Failure) | 2 tests |
| 10 | Scenario S7 (Epoch Stale) | 2 tests |
| 11 | Gate test (MVP verification) | 10 tests |
| 12 | Full suite verification | Regression check |

**Total new tests: ~70**

Remaining scenarios (S2, S3, S4, S6, S8, S13, S14) follow the same pattern as Tasks 8-10 and can be added incrementally after the MVP harness is validated.
