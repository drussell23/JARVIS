# Disease 10 Wiring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the 5 standalone Disease 10 startup sequencing modules into the JARVIS codebase via a new StartupOrchestrator with routing authority FSM, tiered concurrency budget, hybrid readiness prober, event-sourced telemetry, and TUI dashboard integration.

**Architecture:** A new `startup_orchestrator.py` coordinates Disease 10 lifecycle between `unified_supervisor.py` and the standalone modules. A `RoutingAuthorityFSM` enforces single-writer routing authority with fail-closed handoff from boot policy to hybrid router. Event-sourced telemetry feeds a single stream consumed by structured logs, metrics, and TUI.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, JSON schema validation, aiohttp (probe HTTP), Textual (TUI).

**Design doc:** `docs/plans/2026-03-06-disease10-wiring-design.md`

---

## Task Dependency Order

```
Task 1: startup_config.py (foundation)
Task 2: startup_telemetry.py (foundation)
Task 3: routing_authority_fsm.py (core, uses Task 1+2)
Task 4: startup_budget_policy.py (core, uses Task 1+2)
Task 5: gcp_vm_readiness_prober.py (adapter)
Task 6: Modify existing files (prime_router mirror, gcp_hybrid set_active, gcp_vm_manager public)
Task 7: startup_orchestrator.py (ties Tasks 1-6 together)
Task 8: supervisor_tui.py integration (uses Task 2)
Task 9: Integration tests (wiring + recovery)
Task 10: Acceptance tests (full boot scenarios)
```

---

### Task 1: Startup Config — Declarative Gate & Budget Configuration

**Files:**
- Create: `backend/core/startup_config.py`
- Test: `tests/unit/core/test_startup_config.py`

**Context:** This module loads declarative configuration for phase gates and budget policy. All thresholds are env-overridable with range validation. The DAG is validated for soundness at boot time.

**Step 1: Write the tests**

```python
"""Tests for startup_config — declarative gate & budget configuration.

Disease 10 Wiring, Task 1.
"""
from __future__ import annotations

import os
import pytest
from backend.core.startup_config import (
    GateConfig,
    BudgetConfig,
    SoftGatePrecondition,
    StartupConfig,
    load_startup_config,
    ConfigValidationError,
)


class TestGateConfig:
    """Gate configuration loading and validation."""

    def test_default_gate_config(self):
        cfg = load_startup_config()
        assert len(cfg.gates) == 4
        prewarm = cfg.gates["PREWARM_GCP"]
        assert isinstance(prewarm, GateConfig)
        assert prewarm.dependencies == []
        assert prewarm.timeout_s == 45.0
        assert prewarm.on_timeout == "skip"

    def test_core_services_depends_on_prewarm(self):
        cfg = load_startup_config()
        cs = cfg.gates["CORE_SERVICES"]
        assert cs.dependencies == ["PREWARM_GCP"]
        assert cs.on_timeout == "fail"

    def test_full_dependency_chain(self):
        cfg = load_startup_config()
        assert cfg.gates["CORE_READY"].dependencies == ["CORE_SERVICES"]
        assert cfg.gates["DEFERRED_COMPONENTS"].dependencies == ["CORE_READY"]

    def test_env_override_timeout(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GATE_PREWARM_TIMEOUT", "99.0")
        cfg = load_startup_config()
        assert cfg.gates["PREWARM_GCP"].timeout_s == 99.0

    def test_env_override_bounds_enforced(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GATE_PREWARM_TIMEOUT", "0.001")
        with pytest.raises(ConfigValidationError, match="below minimum"):
            load_startup_config()

    def test_env_override_upper_bound(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GATE_PREWARM_TIMEOUT", "99999")
        with pytest.raises(ConfigValidationError, match="above maximum"):
            load_startup_config()


class TestBudgetConfig:
    """Budget configuration loading and validation."""

    def test_default_budget_config(self):
        cfg = load_startup_config()
        b = cfg.budget
        assert isinstance(b, BudgetConfig)
        assert b.max_hard_concurrent == 1
        assert b.max_total_concurrent == 3
        assert set(b.hard_gate_categories) == {"MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"}
        assert set(b.soft_gate_categories) == {"ML_INIT", "GCP_PROVISION"}

    def test_soft_gate_preconditions(self):
        cfg = load_startup_config()
        ml = cfg.budget.soft_gate_preconditions["ML_INIT"]
        assert isinstance(ml, SoftGatePrecondition)
        assert ml.require_phase == "CORE_READY"
        assert ml.require_memory_stable_s == 10.0

    def test_gcp_parallel_allowed(self):
        cfg = load_startup_config()
        assert cfg.budget.gcp_parallel_allowed is True

    def test_env_override_max_hard(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BUDGET_MAX_HARD", "2")
        cfg = load_startup_config()
        assert cfg.budget.max_hard_concurrent == 2

    def test_budget_max_wait(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BUDGET_MAX_WAIT_S", "30.0")
        cfg = load_startup_config()
        assert cfg.budget.max_wait_s == 30.0


class TestDAGValidation:
    """DAG soundness checks at load time."""

    def test_default_dag_is_sound(self):
        cfg = load_startup_config()
        cfg.validate_dag()  # should not raise

    def test_cycle_detection(self):
        cfg = load_startup_config()
        cfg.gates["PREWARM_GCP"] = GateConfig(
            dependencies=["DEFERRED_COMPONENTS"],
            timeout_s=45.0,
            on_timeout="skip",
        )
        with pytest.raises(ConfigValidationError, match="cycle"):
            cfg.validate_dag()

    def test_unknown_dependency_target(self):
        cfg = load_startup_config()
        cfg.gates["CORE_SERVICES"] = GateConfig(
            dependencies=["NONEXISTENT"],
            timeout_s=120.0,
            on_timeout="fail",
        )
        with pytest.raises(ConfigValidationError, match="unknown"):
            cfg.validate_dag()

    def test_unreachable_phase(self):
        cfg = load_startup_config()
        cfg.gates["ISOLATED"] = GateConfig(
            dependencies=[],
            timeout_s=30.0,
            on_timeout="fail",
        )
        # Not technically an error — isolated phases are allowed
        cfg.validate_dag()  # should not raise

    def test_duplicate_phase_detected_at_construction(self):
        """Gate names must be unique — dict keys enforce this naturally."""
        cfg = load_startup_config()
        # Overwriting a key replaces it, no duplicate possible
        assert len(set(cfg.gates.keys())) == len(cfg.gates)


class TestTransitionTimeouts:
    """FSM transition timeout configuration."""

    def test_default_handoff_timeout(self):
        cfg = load_startup_config()
        assert cfg.handoff_timeout_s == 10.0

    def test_default_drain_window(self):
        cfg = load_startup_config()
        assert cfg.drain_window_s == 5.0

    def test_lease_config(self):
        cfg = load_startup_config()
        assert cfg.lease_ttl_s == 120.0
        assert cfg.probe_timeout_s == 15.0
        assert cfg.probe_cache_ttl_s == 3.0
        assert cfg.lease_hysteresis_count == 3

    def test_env_override_handoff_timeout(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HANDOFF_TIMEOUT_S", "20.0")
        cfg = load_startup_config()
        assert cfg.handoff_timeout_s == 20.0
```

**Step 2: Run tests to verify they fail**

```bash
cd /path/to/worktree && python3 -m pytest tests/unit/core/test_startup_config.py -v
```

Expected: FAIL (module not found)

**Step 3: Implement `startup_config.py`**

```python
"""Declarative startup configuration with schema and DAG validation.

Disease 10 Wiring — Task 1.

Loads gate dependency graph, budget policy, and FSM transition timeouts
from defaults with env-var overrides.  All numeric values are range-validated.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "GateConfig",
    "SoftGatePrecondition",
    "BudgetConfig",
    "StartupConfig",
    "ConfigValidationError",
    "load_startup_config",
]


class ConfigValidationError(Exception):
    """Raised when startup config fails validation."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float, min_val: float = 0.1, max_val: float = 3600.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = float(raw)
    if val < min_val:
        raise ConfigValidationError(
            f"{name}={val} below minimum {min_val}"
        )
    if val > max_val:
        raise ConfigValidationError(
            f"{name}={val} above maximum {max_val}"
        )
    return val


def _env_int(name: str, default: int, min_val: int = 0, max_val: int = 100) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = int(raw)
    if val < min_val:
        raise ConfigValidationError(f"{name}={val} below minimum {min_val}")
    if val > max_val:
        raise ConfigValidationError(f"{name}={val} above maximum {max_val}")
    return val


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GateConfig:
    """Configuration for a single phase gate."""
    dependencies: List[str]
    timeout_s: float
    on_timeout: str  # "skip" | "fail"


@dataclass
class SoftGatePrecondition:
    """Precondition for a soft-gate budget category."""
    require_phase: str
    require_memory_stable_s: float = 10.0
    memory_slope_threshold_mb_s: float = 0.5
    memory_sample_interval_s: float = 1.0


@dataclass
class BudgetConfig:
    """Tiered concurrency budget configuration."""
    max_hard_concurrent: int = 1
    max_total_concurrent: int = 3
    hard_gate_categories: List[str] = field(
        default_factory=lambda: ["MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"]
    )
    soft_gate_categories: List[str] = field(
        default_factory=lambda: ["ML_INIT", "GCP_PROVISION"]
    )
    soft_gate_preconditions: Dict[str, SoftGatePrecondition] = field(default_factory=dict)
    gcp_parallel_allowed: bool = True
    max_wait_s: float = 60.0


@dataclass
class StartupConfig:
    """Complete startup configuration."""
    gates: Dict[str, GateConfig] = field(default_factory=dict)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    # FSM transition timeouts
    handoff_timeout_s: float = 10.0
    drain_window_s: float = 5.0

    # Lease configuration
    lease_ttl_s: float = 120.0
    probe_timeout_s: float = 15.0
    probe_cache_ttl_s: float = 3.0
    lease_hysteresis_count: int = 3
    lease_reacquire_delay_s: float = 30.0

    # GCP routing deadline
    gcp_deadline_s: float = 60.0
    cloud_fallback_enabled: bool = True

    # Recovery
    handoff_retry_enabled: bool = False

    # Persistence
    fsm_journal_path: str = ""

    def validate_dag(self) -> None:
        """Validate the gate dependency DAG for soundness."""
        gate_names = set(self.gates.keys())

        # Check for unknown dependency targets
        for name, gate in self.gates.items():
            for dep in gate.dependencies:
                if dep not in gate_names:
                    raise ConfigValidationError(
                        f"Gate '{name}' has unknown dependency '{dep}'"
                    )

        # Cycle detection via DFS
        visited: set = set()
        in_stack: set = set()

        def _dfs(node: str) -> None:
            if node in in_stack:
                raise ConfigValidationError(
                    f"Dependency cycle detected involving '{node}'"
                )
            if node in visited:
                return
            in_stack.add(node)
            for dep in self.gates.get(node, GateConfig([], 0, "fail")).dependencies:
                _dfs(dep)
            in_stack.discard(node)
            visited.add(node)

        for name in gate_names:
            _dfs(name)


def load_startup_config() -> StartupConfig:
    """Load startup configuration from defaults + env overrides."""
    gates = {
        "PREWARM_GCP": GateConfig(
            dependencies=[],
            timeout_s=_env_float("JARVIS_GATE_PREWARM_TIMEOUT", 45.0),
            on_timeout="skip",
        ),
        "CORE_SERVICES": GateConfig(
            dependencies=["PREWARM_GCP"],
            timeout_s=_env_float("JARVIS_GATE_CORE_SERVICES_TIMEOUT", 120.0),
            on_timeout="fail",
        ),
        "CORE_READY": GateConfig(
            dependencies=["CORE_SERVICES"],
            timeout_s=_env_float("JARVIS_GATE_CORE_READY_TIMEOUT", 60.0),
            on_timeout="fail",
        ),
        "DEFERRED_COMPONENTS": GateConfig(
            dependencies=["CORE_READY"],
            timeout_s=_env_float("JARVIS_GATE_DEFERRED_TIMEOUT", 90.0),
            on_timeout="fail",
        ),
    }

    budget = BudgetConfig(
        max_hard_concurrent=_env_int("JARVIS_BUDGET_MAX_HARD", 1, min_val=1, max_val=10),
        max_total_concurrent=_env_int("JARVIS_BUDGET_MAX_TOTAL", 3, min_val=1, max_val=20),
        max_wait_s=_env_float("JARVIS_BUDGET_MAX_WAIT_S", 60.0),
        soft_gate_preconditions={
            "ML_INIT": SoftGatePrecondition(
                require_phase="CORE_READY",
                require_memory_stable_s=_env_float("JARVIS_MEMORY_STABLE_S", 10.0),
                memory_slope_threshold_mb_s=_env_float("JARVIS_MEMORY_SLOPE_THRESHOLD", 0.5),
            ),
        },
    )

    state_dir = os.environ.get("JARVIS_STATE_DIR", "/tmp/jarvis")
    default_journal = os.path.join(state_dir, "startup_fsm_journal.jsonl")

    return StartupConfig(
        gates=gates,
        budget=budget,
        handoff_timeout_s=_env_float("JARVIS_HANDOFF_TIMEOUT_S", 10.0),
        drain_window_s=_env_float("JARVIS_DRAIN_WINDOW_S", 5.0),
        lease_ttl_s=_env_float("JARVIS_LEASE_TTL_S", 120.0),
        probe_timeout_s=_env_float("JARVIS_PROBE_TIMEOUT_S", 15.0),
        probe_cache_ttl_s=_env_float("JARVIS_PROBE_CACHE_TTL", 3.0),
        lease_hysteresis_count=_env_int("JARVIS_LEASE_HYSTERESIS_COUNT", 3, min_val=1, max_val=20),
        lease_reacquire_delay_s=_env_float("JARVIS_LEASE_REACQUIRE_DELAY_S", 30.0),
        gcp_deadline_s=_env_float("JARVIS_GCP_DEADLINE_S", 60.0),
        cloud_fallback_enabled=_env_bool("JARVIS_CLOUD_FALLBACK_ENABLED", True),
        handoff_retry_enabled=_env_bool("JARVIS_HANDOFF_RETRY_ENABLED", False),
        fsm_journal_path=os.environ.get("JARVIS_FSM_JOURNAL_PATH", default_journal),
    )
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/core/test_startup_config.py -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/startup_config.py tests/unit/core/test_startup_config.py
git commit -m "feat(disease10): add declarative startup config with DAG validation (Task 1)"
```

---

### Task 2: Startup Telemetry — Event-Sourced Observability

**Files:**
- Create: `backend/core/startup_telemetry.py`
- Test: `tests/unit/core/test_startup_telemetry.py`

**Context:** Single event stream consumed by structured logger, metrics collector, TUI bridge, and FSM journal. Every Disease 10 component emits `StartupEvent` instances through a `StartupEventBus`. Consumers subscribe at bus creation.

**Step 1: Write the tests**

```python
"""Tests for startup_telemetry — event-sourced observability.

Disease 10 Wiring, Task 2.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import List

import pytest

from backend.core.startup_telemetry import (
    StartupEvent,
    StartupEventBus,
    StructuredLogger,
    MetricsCollector,
    EventConsumer,
)


class TestStartupEvent:
    """Event dataclass properties."""

    def test_event_is_frozen(self):
        evt = StartupEvent(
            trace_id="abc-123",
            event_type="phase_gate",
            timestamp=time.monotonic(),
            wall_clock="2026-03-06T12:00:00Z",
            authority_state="BOOT_POLICY_ACTIVE",
            phase="PREWARM_GCP",
            detail={"status": "passed"},
        )
        with pytest.raises(AttributeError):
            evt.trace_id = "changed"

    def test_event_has_required_fields(self):
        evt = StartupEvent(
            trace_id="t1",
            event_type="lease_probe",
            timestamp=1.0,
            wall_clock="2026-01-01T00:00:00Z",
            authority_state="BOOT_POLICY_ACTIVE",
            phase=None,
            detail={},
        )
        assert evt.trace_id == "t1"
        assert evt.phase is None


class TestStartupEventBus:
    """Event bus broadcast and consumer management."""

    @pytest.fixture
    def bus(self) -> StartupEventBus:
        return StartupEventBus(trace_id="test-trace")

    def _make_event(self, bus: StartupEventBus, event_type: str = "test") -> StartupEvent:
        return bus.create_event(event_type=event_type, detail={"key": "val"})

    async def test_emit_delivers_to_all_consumers(self, bus: StartupEventBus):
        received_a: List[StartupEvent] = []
        received_b: List[StartupEvent] = []

        class ConsumerA(EventConsumer):
            async def consume(self, event: StartupEvent) -> None:
                received_a.append(event)

        class ConsumerB(EventConsumer):
            async def consume(self, event: StartupEvent) -> None:
                received_b.append(event)

        bus.subscribe(ConsumerA())
        bus.subscribe(ConsumerB())

        evt = self._make_event(bus)
        await bus.emit(evt)

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0] is evt

    async def test_emit_with_no_consumers_does_not_error(self, bus: StartupEventBus):
        evt = self._make_event(bus)
        await bus.emit(evt)  # should not raise

    async def test_consumer_error_does_not_block_others(self, bus: StartupEventBus):
        received: List[StartupEvent] = []

        class BadConsumer(EventConsumer):
            async def consume(self, event: StartupEvent) -> None:
                raise RuntimeError("consumer failed")

        class GoodConsumer(EventConsumer):
            async def consume(self, event: StartupEvent) -> None:
                received.append(event)

        bus.subscribe(BadConsumer())
        bus.subscribe(GoodConsumer())

        evt = self._make_event(bus)
        await bus.emit(evt)  # bad consumer fails, good still receives
        assert len(received) == 1

    def test_create_event_sets_trace_id(self, bus: StartupEventBus):
        evt = bus.create_event(event_type="test", detail={})
        assert evt.trace_id == "test-trace"
        assert evt.timestamp > 0

    def test_event_history_returns_copy(self, bus: StartupEventBus):
        bus._history.append(self._make_event(bus))
        h1 = bus.event_history
        h1.clear()
        assert len(bus.event_history) == 1

    async def test_emit_appends_to_history(self, bus: StartupEventBus):
        evt = self._make_event(bus)
        await bus.emit(evt)
        assert len(bus.event_history) == 1
        assert bus.event_history[0] is evt


class TestStructuredLogger:
    """JSON-line structured logging consumer."""

    async def test_logs_event_as_json(self, tmp_path):
        log_file = tmp_path / "startup.jsonl"
        logger = StructuredLogger(str(log_file))
        evt = StartupEvent(
            trace_id="t1",
            event_type="phase_gate",
            timestamp=1.0,
            wall_clock="2026-01-01T00:00:00Z",
            authority_state="BOOT_POLICY_ACTIVE",
            phase="PREWARM_GCP",
            detail={"status": "passed"},
        )
        await logger.consume(evt)
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["trace_id"] == "t1"
        assert data["event_type"] == "phase_gate"
        assert data["detail"]["status"] == "passed"


class TestMetricsCollector:
    """Metrics aggregation consumer."""

    async def test_counts_events_by_type(self):
        mc = MetricsCollector()
        for _ in range(3):
            evt = StartupEvent(
                trace_id="t1",
                event_type="phase_gate",
                timestamp=time.monotonic(),
                wall_clock="",
                authority_state="BOOT_POLICY_ACTIVE",
                phase=None,
                detail={},
            )
            await mc.consume(evt)
        evt2 = StartupEvent(
            trace_id="t1",
            event_type="lease_probe",
            timestamp=time.monotonic(),
            wall_clock="",
            authority_state="BOOT_POLICY_ACTIVE",
            phase=None,
            detail={},
        )
        await mc.consume(evt2)
        assert mc.counts["phase_gate"] == 3
        assert mc.counts["lease_probe"] == 1

    async def test_tracks_phase_durations(self):
        mc = MetricsCollector()
        evt = StartupEvent(
            trace_id="t1",
            event_type="phase_gate",
            timestamp=1.0,
            wall_clock="",
            authority_state="BOOT_POLICY_ACTIVE",
            phase="PREWARM_GCP",
            detail={"duration_s": 2.5, "status": "passed"},
        )
        await mc.consume(evt)
        assert mc.phase_durations["PREWARM_GCP"] == 2.5

    def test_snapshot_returns_copy(self):
        mc = MetricsCollector()
        snap = mc.snapshot()
        assert isinstance(snap, dict)
        assert "counts" in snap
        assert "phase_durations" in snap
```

**Step 2: Run tests, verify fail**

**Step 3: Implement `startup_telemetry.py`**

Key implementation points:
- `StartupEvent` is a `@dataclass(frozen=True)` with fields: `trace_id`, `event_type`, `timestamp`, `wall_clock`, `authority_state`, `phase` (Optional), `detail` (Dict).
- `EventConsumer` is an ABC with `async def consume(event: StartupEvent) -> None`.
- `StartupEventBus` holds a list of consumers, a `trace_id`, and an `_history` list. `emit()` broadcasts to all consumers (catching per-consumer errors). `create_event()` factory method stamps `trace_id`, `timestamp`, `wall_clock`, and a placeholder `authority_state` (set by orchestrator).
- `StructuredLogger` writes JSON lines to a file (append mode).
- `MetricsCollector` maintains `counts: Dict[str, int]`, `phase_durations: Dict[str, float]`, `budget_wait_times: List[float]`. `snapshot()` returns a copy.

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/startup_telemetry.py tests/unit/core/test_startup_telemetry.py
git commit -m "feat(disease10): add event-sourced startup telemetry (Task 2)"
```

---

### Task 3: Routing Authority FSM

**Files:**
- Create: `backend/core/routing_authority_fsm.py`
- Test: `tests/unit/core/test_routing_authority_fsm.py`

**Context:** Explicit state machine enforcing single-writer routing authority. States: `BOOT_POLICY_ACTIVE`, `HANDOFF_PENDING`, `HYBRID_ACTIVE`, `HANDOFF_FAILED`. Each transition has guard checks evaluated in deterministic order (cheap/static first, then dynamic). Transitions are journaled for restart recovery.

**Step 1: Write the tests**

```python
"""Tests for routing_authority_fsm — fail-closed authority state machine.

Disease 10 Wiring, Task 3.
"""
from __future__ import annotations

import json
import time
from typing import Dict, Any

import pytest

from backend.core.routing_authority_fsm import (
    AuthorityState,
    TransitionResult,
    TransitionFailure,
    GuardResult,
    RoutingAuthorityFSM,
)


def _passing_guards() -> Dict[str, bool]:
    return {
        "core_ready_passed": True,
        "contracts_valid": True,
        "invariants_clean": True,
        "hybrid_router_ready": True,
        "lease_or_local_ready": True,
        "readiness_contract_passed": True,
        "no_in_flight_requests": True,
    }


class TestFSMInitialState:

    def test_starts_in_boot_policy_active(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_authority_holder_is_boot_policy(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.authority_holder == "boot_policy"

    def test_transition_log_is_empty(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.transition_log == []


class TestBootToHandoff:

    def test_begin_handoff_succeeds_with_all_guards(self):
        fsm = RoutingAuthorityFSM()
        guards = {
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        }
        result = fsm.begin_handoff(guards)
        assert result.success is True
        assert fsm.state == AuthorityState.HANDOFF_PENDING
        assert fsm.authority_holder == "handoff_controller"

    def test_begin_handoff_fails_on_unmet_guard(self):
        fsm = RoutingAuthorityFSM()
        guards = {
            "core_ready_passed": False,
            "contracts_valid": True,
            "invariants_clean": True,
        }
        result = fsm.begin_handoff(guards)
        assert result.success is False
        assert result.failed_guard == "core_ready_passed"
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_begin_handoff_from_wrong_state_fails(self):
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        # Already in HANDOFF_PENDING — can't begin again
        result = fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        assert result.success is False


class TestHandoffToHybrid:

    @pytest.fixture
    def fsm_in_handoff(self) -> RoutingAuthorityFSM:
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        return fsm

    def test_complete_handoff_succeeds(self, fsm_in_handoff):
        guards = _passing_guards()
        result = fsm_in_handoff.complete_handoff(guards)
        assert result.success is True
        assert fsm_in_handoff.state == AuthorityState.HYBRID_ACTIVE
        assert fsm_in_handoff.authority_holder == "hybrid_router"

    def test_complete_handoff_fails_on_unmet_guard(self, fsm_in_handoff):
        guards = _passing_guards()
        guards["hybrid_router_ready"] = False
        result = fsm_in_handoff.complete_handoff(guards)
        assert result.success is False
        assert result.failed_guard == "hybrid_router_ready"
        assert fsm_in_handoff.state == AuthorityState.HANDOFF_FAILED

    def test_handoff_failed_auto_rollback(self, fsm_in_handoff):
        guards = _passing_guards()
        guards["invariants_clean"] = False
        fsm_in_handoff.complete_handoff(guards)
        assert fsm_in_handoff.state == AuthorityState.HANDOFF_FAILED
        # Rollback
        result = fsm_in_handoff.rollback("guard_failure")
        assert result.success is True
        assert fsm_in_handoff.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm_in_handoff.authority_holder == "boot_policy"


class TestCatastrophicRollback:

    @pytest.fixture
    def fsm_hybrid(self) -> RoutingAuthorityFSM:
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        fsm.complete_handoff(_passing_guards())
        return fsm

    def test_rollback_from_hybrid_on_lease_loss(self, fsm_hybrid):
        result = fsm_hybrid.rollback("lease_loss")
        assert result.success is True
        assert fsm_hybrid.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm_hybrid.authority_holder == "boot_policy"

    def test_rollback_records_cause(self, fsm_hybrid):
        fsm_hybrid.rollback("readiness_regression")
        log = fsm_hybrid.transition_log
        rollback_entry = [e for e in log if e["to_state"] == "BOOT_POLICY_ACTIVE" and "rollback" in e.get("cause", "")]
        assert len(rollback_entry) >= 1

    def test_rollback_from_boot_policy_is_noop(self):
        fsm = RoutingAuthorityFSM()
        result = fsm.rollback("spurious")
        assert result.success is True  # already in boot policy
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE


class TestGuardEvaluationOrder:

    def test_guards_evaluated_in_deterministic_order(self):
        fsm = RoutingAuthorityFSM()
        # All fail — should fail on the first one (alphabetical/priority order)
        guards = {
            "core_ready_passed": False,
            "contracts_valid": False,
            "invariants_clean": False,
        }
        result = fsm.begin_handoff(guards)
        # First guard in priority order should be reported
        assert result.failed_guard == "core_ready_passed"


class TestTokenUniqueness:

    def test_only_one_authority_token(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.authority_holder == "boot_policy"
        fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        assert fsm.authority_holder == "handoff_controller"
        # boot_policy no longer holds authority
        assert fsm.is_authority("boot_policy") is False
        assert fsm.is_authority("handoff_controller") is True


class TestTransitionLog:

    def test_transitions_are_logged(self):
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        log = fsm.transition_log
        assert len(log) == 1
        assert log[0]["from_state"] == "BOOT_POLICY_ACTIVE"
        assert log[0]["to_state"] == "HANDOFF_PENDING"

    def test_transition_log_is_a_copy(self):
        fsm = RoutingAuthorityFSM()
        log1 = fsm.transition_log
        log1.append({"fake": True})
        assert len(fsm.transition_log) == 0


class TestJournalPersistence:

    def test_journal_write(self, tmp_path):
        journal_path = str(tmp_path / "fsm.jsonl")
        fsm = RoutingAuthorityFSM(journal_path=journal_path)
        fsm.begin_handoff({
            "core_ready_passed": True,
            "contracts_valid": True,
            "invariants_clean": True,
        })
        with open(journal_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["to_state"] == "HANDOFF_PENDING"

    def test_journal_recovery(self, tmp_path):
        journal_path = str(tmp_path / "fsm.jsonl")
        # Write a journal entry indicating HANDOFF_PENDING
        entry = {
            "from_state": "BOOT_POLICY_ACTIVE",
            "to_state": "HANDOFF_PENDING",
            "timestamp": time.monotonic(),
            "cause": "begin_handoff",
        }
        with open(journal_path, "w") as f:
            f.write(json.dumps(entry) + "\n")
        # FSM should recover to BOOT_POLICY_ACTIVE (safe default on restart)
        fsm = RoutingAuthorityFSM(journal_path=journal_path)
        # Restart during HANDOFF_PENDING -> rollback to boot policy
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE
```

**Step 2: Run tests, verify fail**

**Step 3: Implement `routing_authority_fsm.py`**

Key implementation points:
- `AuthorityState` enum: `BOOT_POLICY_ACTIVE`, `HANDOFF_PENDING`, `HYBRID_ACTIVE`, `HANDOFF_FAILED`.
- `TransitionResult` frozen dataclass: `success: bool`, `from_state`, `to_state`, `failed_guard: Optional[str]`, `timestamp`.
- `RoutingAuthorityFSM.__init__(journal_path=None)`: sets state to `BOOT_POLICY_ACTIVE`, loads journal if exists (and rolls back to safe state).
- `begin_handoff(guards: Dict[str, bool]) -> TransitionResult`: validates current state is `BOOT_POLICY_ACTIVE`, evaluates guards in priority order (`core_ready_passed` > `contracts_valid` > `invariants_clean`), transitions to `HANDOFF_PENDING` or stays put.
- `complete_handoff(guards: Dict[str, bool]) -> TransitionResult`: validates `HANDOFF_PENDING`, evaluates full guard set (cheap first: `contracts_valid`, `invariants_clean`; then dynamic: `hybrid_router_ready`, `lease_or_local_ready`, `readiness_contract_passed`; then drain: `no_in_flight_requests`). On failure → `HANDOFF_FAILED`.
- `rollback(cause: str) -> TransitionResult`: any non-BOOT state → `BOOT_POLICY_ACTIVE`.
- `is_authority(holder: str) -> bool`: checks `self._authority_holder == holder`.
- `transition_log` property returns copy of `_log` list.
- `_journal_write(entry)`: appends JSON line to journal file if path set.
- On init with existing journal: reads last entry, if state was `HANDOFF_PENDING` or `HANDOFF_FAILED`, rolls back to `BOOT_POLICY_ACTIVE` (fail-closed restart recovery).

Authority holders:
- `BOOT_POLICY_ACTIVE` → `"boot_policy"`
- `HANDOFF_PENDING` → `"handoff_controller"`
- `HYBRID_ACTIVE` → `"hybrid_router"`
- `HANDOFF_FAILED` → `"handoff_controller"` (until rollback)

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/routing_authority_fsm.py tests/unit/core/test_routing_authority_fsm.py
git commit -m "feat(disease10): add routing authority FSM with fail-closed handoff (Task 3)"
```

---

### Task 4: Startup Budget Policy — Tiered Enforcement

**Files:**
- Create: `backend/core/startup_budget_policy.py`
- Test: `tests/unit/core/test_startup_budget_policy.py`

**Context:** Wraps `StartupConcurrencyBudget` with tiered enforcement: hard semaphore (max 1) for RAM killers, total semaphore (max 3) for all categories. Soft gates have preconditions (phase gate status, memory stability). Starvation protection via per-category max wait. Emits telemetry events.

**Step 1: Write the tests**

```python
"""Tests for startup_budget_policy — tiered concurrency enforcement.

Disease 10 Wiring, Task 4.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.startup_budget_policy import (
    StartupBudgetPolicy,
    BudgetAcquisitionError,
    PreconditionNotMetError,
)
from backend.core.startup_config import BudgetConfig, SoftGatePrecondition
from backend.core.startup_concurrency_budget import HeavyTaskCategory


@pytest.fixture
def default_config() -> BudgetConfig:
    return BudgetConfig(
        max_hard_concurrent=1,
        max_total_concurrent=3,
        max_wait_s=5.0,
        soft_gate_preconditions={
            "ML_INIT": SoftGatePrecondition(
                require_phase="CORE_READY",
                require_memory_stable_s=0.0,  # disable for unit tests
            ),
        },
    )


@pytest.fixture
def policy(default_config) -> StartupBudgetPolicy:
    return StartupBudgetPolicy(default_config)


class TestHardGateEnforcement:

    async def test_single_hard_task_acquires(self, policy):
        async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "prime-model") as slot:
            assert slot is not None
            assert policy.active_count == 1

    async def test_two_hard_tasks_serialized(self, policy):
        """Second hard task blocks until first releases."""
        order = []

        async def task(name, delay):
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, name):
                order.append(f"{name}_start")
                await asyncio.sleep(delay)
                order.append(f"{name}_end")

        t1 = asyncio.create_task(task("a", 0.05))
        await asyncio.sleep(0.01)  # ensure t1 starts first
        t2 = asyncio.create_task(task("b", 0.01))
        await asyncio.gather(t1, t2)

        assert order == ["a_start", "a_end", "b_start", "b_end"]

    async def test_hard_and_soft_can_overlap(self, policy):
        """GCP_PROVISION (soft) can run alongside MODEL_LOAD (hard)."""
        active_together = False

        async def hard_task():
            nonlocal active_together
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "model"):
                await asyncio.sleep(0.05)
                if policy.active_count > 1:
                    active_together = True

        async def soft_task():
            nonlocal active_together
            async with policy.acquire(HeavyTaskCategory.GCP_PROVISION, "gcp"):
                await asyncio.sleep(0.05)
                if policy.active_count > 1:
                    active_together = True

        await asyncio.gather(hard_task(), soft_task())
        assert active_together is True


class TestSoftGatePreconditions:

    async def test_ml_init_blocked_without_phase(self, policy):
        """ML_INIT requires CORE_READY phase — fails without it."""
        with pytest.raises(PreconditionNotMetError, match="CORE_READY"):
            async with policy.acquire(HeavyTaskCategory.ML_INIT, "ecapa"):
                pass

    async def test_ml_init_allowed_after_phase_signal(self, policy):
        """After signaling CORE_READY, ML_INIT proceeds."""
        policy.signal_phase_reached("CORE_READY")
        async with policy.acquire(HeavyTaskCategory.ML_INIT, "ecapa") as slot:
            assert slot is not None


class TestStarvationProtection:

    async def test_max_wait_exceeded_raises(self):
        config = BudgetConfig(
            max_hard_concurrent=1,
            max_total_concurrent=1,
            max_wait_s=0.05,  # very short for test
        )
        policy = StartupBudgetPolicy(config)

        async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "blocker"):
            # Second task should timeout waiting
            with pytest.raises(BudgetAcquisitionError, match="timed out"):
                async with policy.acquire(HeavyTaskCategory.REACTOR_LAUNCH, "starved", timeout=0.05):
                    pass


class TestLeakHardening:

    async def test_slot_released_on_exception(self, policy):
        """Slots must be released even if task body raises."""
        with pytest.raises(ValueError):
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "fail"):
                raise ValueError("boom")
        assert policy.active_count == 0

    async def test_slot_released_on_cancellation(self, policy):
        """Slots must be released on task cancellation."""
        started = asyncio.Event()

        async def long_task():
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "cancel-me"):
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(long_task())
        await started.wait()
        assert policy.active_count == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Allow event loop to process cleanup
        await asyncio.sleep(0.01)
        assert policy.active_count == 0


class TestObservability:

    async def test_history_records_completed(self, policy):
        async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "test"):
            await asyncio.sleep(0.01)
        history = policy.history
        assert len(history) == 1
        assert history[0].name == "test"
        assert history[0].duration_s > 0

    async def test_peak_concurrent_tracked(self, policy):
        barrier = asyncio.Event()

        async def task(cat, name):
            async with policy.acquire(cat, name):
                barrier.set()
                await asyncio.sleep(0.05)

        t1 = asyncio.create_task(task(HeavyTaskCategory.MODEL_LOAD, "a"))
        t2 = asyncio.create_task(task(HeavyTaskCategory.GCP_PROVISION, "b"))
        await asyncio.gather(t1, t2)
        assert policy.peak_concurrent >= 2
```

**Step 2: Run tests, verify fail**

**Step 3: Implement `startup_budget_policy.py`**

Key implementation points:
- Wraps `StartupConcurrencyBudget` but adds a second `asyncio.Semaphore` for hard-gate categories.
- `acquire()` is an `@asynccontextmanager` that: (1) checks preconditions for soft gates, (2) acquires total semaphore, (3) acquires hard semaphore if hard category, (4) yields slot, (5) releases in reverse order under `try/finally`.
- `signal_phase_reached(phase: str)` adds to a `_reached_phases: Set[str]`.
- Precondition check: for each category in `soft_gate_preconditions`, verify `require_phase` is in `_reached_phases`.
- `BudgetAcquisitionError` and `PreconditionNotMetError` are custom exceptions.
- Delegates `active_count`, `peak_concurrent`, `history` to the underlying `StartupConcurrencyBudget`.

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/startup_budget_policy.py tests/unit/core/test_startup_budget_policy.py
git commit -m "feat(disease10): add tiered startup budget policy (Task 4)"
```

---

### Task 5: GCP VM Readiness Prober — Hybrid Adapter

**Files:**
- Create: `backend/core/gcp_vm_readiness_prober.py`
- Test: `tests/unit/core/test_gcp_vm_readiness_prober.py`

**Context:** Concrete `ReadinessProber` adapter. Delegates `probe_health` and `probe_capabilities` to GCPVMManager methods. Adds standalone `probe_warm_model` via HTTP POST to `/v1/warm_check`. Includes probe result caching (configurable TTL) for health/capabilities to prevent probe storms. Warm model probe is never cached.

**Step 1: Write the tests**

Tests use a `FakeVMManager` that stubs `ping_health()` and `check_lineage()` to avoid real GCP calls. The warm model probe uses a fake HTTP endpoint (or mocked `aiohttp.ClientSession`).

```python
"""Tests for gcp_vm_readiness_prober — hybrid adapter.

Disease 10 Wiring, Task 5.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.gcp_readiness_lease import (
    HandshakeResult,
    HandshakeStep,
    ReadinessFailureClass,
)
from backend.core.gcp_vm_readiness_prober import GCPVMReadinessProber


class FakeVMManager:
    """Stub for GCPVMManager with controllable health/lineage responses."""

    def __init__(
        self,
        health_ready: bool = True,
        lineage_ok: bool = True,
        health_status: Optional[Dict] = None,
    ):
        self._health_ready = health_ready
        self._lineage_ok = lineage_ok
        self._health_status = health_status or {}

    async def ping_health(
        self, host: str, port: int, timeout: float = 10.0
    ) -> Tuple[Any, Dict]:
        if not self._health_ready:
            from backend.core.gcp_vm_manager import HealthVerdict
            return HealthVerdict.UNREACHABLE, {}
        from backend.core.gcp_vm_manager import HealthVerdict
        return HealthVerdict.READY, self._health_status

    async def check_lineage(
        self, instance_name: str, vm_metadata: Optional[Dict] = None
    ) -> Tuple[bool, str]:
        if not self._lineage_ok:
            return True, "golden image mismatch"  # should_recreate=True
        return False, "lineage matches"  # should_recreate=False


class TestProbeHealth:

    async def test_healthy_vm_returns_passed(self):
        prober = GCPVMReadinessProber(FakeVMManager(health_ready=True))
        result = await prober.probe_health("10.0.0.1", 8000, timeout=5.0)
        assert result.passed is True
        assert result.step == HandshakeStep.HEALTH

    async def test_unreachable_vm_returns_failed(self):
        prober = GCPVMReadinessProber(FakeVMManager(health_ready=False))
        result = await prober.probe_health("10.0.0.1", 8000, timeout=5.0)
        assert result.passed is False
        assert result.failure_class == ReadinessFailureClass.NETWORK

    async def test_health_result_is_cached(self):
        mgr = FakeVMManager(health_ready=True)
        prober = GCPVMReadinessProber(mgr, probe_cache_ttl=1.0)
        r1 = await prober.probe_health("10.0.0.1", 8000, timeout=5.0)
        mgr._health_ready = False  # flip underlying state
        r2 = await prober.probe_health("10.0.0.1", 8000, timeout=5.0)
        assert r2.passed is True  # still cached


class TestProbeCapabilities:

    async def test_matching_lineage_passes(self):
        prober = GCPVMReadinessProber(FakeVMManager(lineage_ok=True))
        result = await prober.probe_capabilities("10.0.0.1", 8000, timeout=5.0)
        assert result.passed is True
        assert result.step == HandshakeStep.CAPABILITIES

    async def test_mismatched_lineage_fails(self):
        prober = GCPVMReadinessProber(FakeVMManager(lineage_ok=False))
        result = await prober.probe_capabilities("10.0.0.1", 8000, timeout=5.0)
        assert result.passed is False
        assert result.failure_class == ReadinessFailureClass.SCHEMA_MISMATCH


class TestProbeWarmModel:

    async def test_warm_probe_success(self):
        prober = GCPVMReadinessProber(FakeVMManager())
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"model": "loaded", "latency_ms": 50})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await prober.probe_warm_model("10.0.0.1", 8000, timeout=5.0)
        assert result.passed is True
        assert result.step == HandshakeStep.WARM_MODEL

    async def test_warm_probe_timeout(self):
        prober = GCPVMReadinessProber(FakeVMManager())

        async def slow_post(*args, **kwargs):
            await asyncio.sleep(10)

        mock_session = AsyncMock()
        mock_session.post = slow_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await prober.probe_warm_model("10.0.0.1", 8000, timeout=0.05)
        assert result.passed is False
        assert result.failure_class == ReadinessFailureClass.TIMEOUT

    async def test_warm_probe_not_cached(self):
        """Warm model probe must not be cached."""
        prober = GCPVMReadinessProber(FakeVMManager(), probe_cache_ttl=10.0)
        # Warm probe should always hit the endpoint
        call_count = 0

        async def counting_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value={"ok": True})
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            return mock_resp

        mock_session = AsyncMock()
        mock_session.post = counting_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await prober.probe_warm_model("10.0.0.1", 8000, timeout=5.0)
            await prober.probe_warm_model("10.0.0.1", 8000, timeout=5.0)
        assert call_count == 2
```

**Step 2: Run tests, verify fail**

**Step 3: Implement `gcp_vm_readiness_prober.py`**

Key implementation points:
- Inherits from `ReadinessProber` ABC (from `gcp_readiness_lease.py`).
- Constructor takes `vm_manager` (duck-typed — needs `ping_health()` and `check_lineage()`) and `probe_cache_ttl: float`.
- `_cache: Dict[HandshakeStep, Tuple[float, HandshakeResult]]` for health/capabilities.
- `probe_health()`: calls `vm_manager.ping_health()`, maps `HealthVerdict.READY` → passed, others → failed with `NETWORK` class. Caches result.
- `probe_capabilities()`: calls `vm_manager.check_lineage()`, maps `should_recreate=False` → passed, `True` → failed with `SCHEMA_MISMATCH`. Caches result.
- `probe_warm_model()`: standalone `aiohttp.ClientSession.post()` to `http://{host}:{port}/v1/warm_check`, wrapped in `asyncio.wait_for(timeout)`. Never cached. Maps 200 → passed, timeout → `TIMEOUT`, other → `NETWORK`.

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_readiness_prober.py tests/unit/core/test_gcp_vm_readiness_prober.py
git commit -m "feat(disease10): add hybrid GCP VM readiness prober adapter (Task 5)"
```

---

### Task 6: Modify Existing Files — Mirror Mode & Public Methods

**Files:**
- Modify: `backend/core/prime_router.py` (add mirror mode)
- Modify: `backend/core/gcp_hybrid_prime_router.py` (add set_active)
- Modify: `backend/core/gcp_vm_manager.py` (expose public methods)
- Test: `tests/unit/core/test_prime_router_mirror.py`

**Context:** PrimeRouter gets a `_mirror_mode` flag with a guard decorator that blocks all mutating methods when active. GCPHybridPrimeRouter gets `set_active(bool)`. GCPVMManager renames two private methods to public for the prober adapter.

**Step 1: Write the tests for mirror mode**

```python
"""Tests for PrimeRouter mirror mode.

Disease 10 Wiring, Task 6.
"""
from __future__ import annotations

import pytest

from backend.core.prime_router import PrimeRouter, MirrorModeError


class TestMirrorMode:

    def test_mirror_mode_default_off(self):
        router = PrimeRouter()
        assert router.mirror_mode is False

    def test_set_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        assert router.mirror_mode is True

    async def test_promote_blocked_in_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        with pytest.raises(MirrorModeError):
            await router.promote_gcp_endpoint("10.0.0.1", 8000)

    async def test_demote_blocked_in_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        with pytest.raises(MirrorModeError):
            await router.demote_gcp_endpoint()

    def test_decide_route_blocked_in_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        with pytest.raises(MirrorModeError):
            router._decide_route()

    def test_mirror_mode_can_be_disabled(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        router.set_mirror_mode(False)
        # Should not raise
        router._decide_route()

    def test_mirror_decisions_counter(self):
        router = PrimeRouter()
        assert router.mirror_decisions_issued == 0
```

**Step 2: Run tests, verify fail**

**Step 3: Implement changes**

**`prime_router.py` changes (~30 lines):**

Add at module level (after imports):
```python
class MirrorModeError(RuntimeError):
    """Raised when a mutating method is called in mirror mode."""
```

Add to `PrimeRouter.__init__()` (after `self._transition_in_flight`):
```python
        # Disease 10: Mirror mode — blocks all mutating methods when active
        self._mirror_mode: bool = False
        self._mirror_decisions_issued: int = 0
```

Add methods to `PrimeRouter`:
```python
    @property
    def mirror_mode(self) -> bool:
        return self._mirror_mode

    @property
    def mirror_decisions_issued(self) -> int:
        return self._mirror_decisions_issued

    def set_mirror_mode(self, enabled: bool) -> None:
        self._mirror_mode = enabled
        if enabled:
            logger.info("[PrimeRouter] Mirror mode ENABLED — all mutations blocked")
        else:
            logger.info("[PrimeRouter] Mirror mode DISABLED")

    def _guard_mirror(self, method_name: str) -> None:
        if self._mirror_mode:
            raise MirrorModeError(
                f"PrimeRouter.{method_name}() blocked: mirror mode active"
            )
```

Add guard calls at the top of `_decide_route()`, `promote_gcp_endpoint()`, `demote_gcp_endpoint()`:
```python
        self._guard_mirror("_decide_route")  # or "promote_gcp_endpoint" etc.
```

**`gcp_hybrid_prime_router.py` changes (~20 lines):**

Add to `GCPHybridPrimeRouter.__init__()`:
```python
        # Disease 10: Active flag — orchestrator controls when this router is authoritative
        self._disease10_active: bool = False
```

Add method:
```python
    def set_active(self, active: bool) -> None:
        """Disease 10: Set whether this router is the active routing authority."""
        self._disease10_active = active
        self.logger.info(
            "[GCPHybridPrimeRouter] Disease 10 active=%s", active
        )

    @property
    def is_disease10_active(self) -> bool:
        return self._disease10_active
```

**`gcp_vm_manager.py` changes (~15 lines):**

Add public wrapper methods (keep originals unchanged):
```python
    async def ping_health(
        self, host: str, port: int, timeout: float = 10.0
    ) -> Tuple[HealthVerdict, Dict]:
        """Public API for Disease 10 readiness prober."""
        return await self._ping_health_endpoint(host, port, timeout=timeout)

    async def check_lineage(
        self, instance_name: str, vm_metadata: Optional[Dict] = None
    ) -> Tuple[bool, str]:
        """Public API for Disease 10 readiness prober."""
        return await self._check_vm_golden_image_lineage(instance_name, vm_metadata)
```

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/prime_router.py backend/core/gcp_hybrid_prime_router.py \
       backend/core/gcp_vm_manager.py tests/unit/core/test_prime_router_mirror.py
git commit -m "feat(disease10): add mirror mode to PrimeRouter, set_active to hybrid router (Task 6)"
```

---

### Task 7: Startup Orchestrator — The Coordinator

**Files:**
- Create: `backend/core/startup_orchestrator.py`
- Test: `tests/unit/core/test_startup_orchestrator.py`

**Context:** This is the main integration module. Creates all Disease 10 components, wires signals between them, manages the authority FSM lifecycle, and provides the interface that `unified_supervisor.py` calls at each phase boundary.

**Step 1: Write the tests**

```python
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
```

**Step 2: Run tests, verify fail**

**Step 3: Implement `startup_orchestrator.py`**

Key implementation:
- Constructor takes `StartupConfig` and `ReadinessProber`.
- Creates internally: `PhaseGateCoordinator`, `StartupBudgetPolicy`, `GCPReadinessLease`, `StartupRoutingPolicy`, `BootInvariantChecker`, `RoutingAuthorityFSM`, `StartupEventBus`.
- Phase methods: `resolve_phase(name, detail)`, `skip_phase(name, reason)`, `fail_phase(name, reason, detail)` — delegate to `PhaseGateCoordinator` with telemetry emission.
- GCP methods: `acquire_gcp_lease(host, port)`, `revoke_gcp_lease(reason)`, `handle_lease_loss(cause)` — manage lease + signal routing policy + check invariants.
- Authority: `attempt_handoff()` — evaluates guards, drives FSM. `handle_lease_loss()` triggers rollback if HYBRID_ACTIVE.
- Budget: `budget_acquire(category, name, timeout)` — delegates to `StartupBudgetPolicy`.
- Invariants: `check_invariants(overrides)` — builds state dict from current orchestrator state, runs `BootInvariantChecker.check_all()`.
- Telemetry: every public method emits a `StartupEvent` through the bus.

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/startup_orchestrator.py tests/unit/core/test_startup_orchestrator.py
git commit -m "feat(disease10): add StartupOrchestrator lifecycle coordinator (Task 7)"
```

---

### Task 8: TUI Dashboard Integration

**Files:**
- Modify: `backend/core/supervisor_tui.py`
- Test: `tests/unit/core/test_startup_tui_widgets.py`

**Context:** Add inline startup summary widget (shown during boot) and detail drill-down panel (post-boot). Both consume the `StartupEventBus` via a `TUIBridge` consumer. The inline widget shows: current phase, authority state, budget occupancy, lease status, invariant results. The detail panel shows: phase timeline, budget contention, lease history, handoff trace.

**Step 1: Write the tests**

Test the `TUIBridge` consumer and the data models it produces for the widgets (don't test Textual rendering directly — test the data layer).

```python
"""Tests for TUI startup dashboard data bridge.

Disease 10 Wiring, Task 8.
"""
from __future__ import annotations

import time
import pytest

from backend.core.startup_telemetry import StartupEvent, StartupEventBus
from backend.core.supervisor_tui_bridge import (
    TUIBridge,
    InlineSummary,
    DetailSnapshot,
)


@pytest.fixture
def bridge() -> TUIBridge:
    return TUIBridge()


def _make_event(event_type: str, detail: dict, phase: str = None) -> StartupEvent:
    return StartupEvent(
        trace_id="test",
        event_type=event_type,
        timestamp=time.monotonic(),
        wall_clock="2026-03-06T12:00:00Z",
        authority_state="BOOT_POLICY_ACTIVE",
        phase=phase,
        detail=detail,
    )


class TestInlineSummary:

    async def test_phase_gate_updates_current_phase(self, bridge):
        evt = _make_event("phase_gate", {"status": "passed", "duration_s": 1.0}, phase="PREWARM_GCP")
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.last_resolved_phase == "PREWARM_GCP"

    async def test_budget_acquire_updates_occupancy(self, bridge):
        evt = _make_event("budget_acquire", {
            "category": "MODEL_LOAD",
            "name": "prime",
            "queue_depth": 0,
            "hard_slot": True,
        })
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.budget_active > 0

    async def test_lease_acquired_updates_status(self, bridge):
        evt = _make_event("lease_acquired", {
            "host": "10.0.0.1",
            "port": 8000,
            "lease_epoch": 1,
            "ttl_s": 120.0,
        })
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.lease_status == "ACTIVE"

    async def test_authority_transition_updates_state(self, bridge):
        evt = _make_event("authority_transition", {
            "from_state": "BOOT_POLICY_ACTIVE",
            "to_state": "HANDOFF_PENDING",
        })
        evt = StartupEvent(
            trace_id="test",
            event_type="authority_transition",
            timestamp=time.monotonic(),
            wall_clock="",
            authority_state="HANDOFF_PENDING",
            phase=None,
            detail={"from_state": "BOOT_POLICY_ACTIVE", "to_state": "HANDOFF_PENDING"},
        )
        await bridge.consume(evt)
        summary = bridge.inline_summary
        assert summary.authority_state == "HANDOFF_PENDING"


class TestDetailSnapshot:

    async def test_phase_timeline_accumulates(self, bridge):
        for phase in ["PREWARM_GCP", "CORE_SERVICES"]:
            evt = _make_event("phase_gate", {
                "status": "passed",
                "duration_s": 1.5,
            }, phase=phase)
            await bridge.consume(evt)
        detail = bridge.detail_snapshot
        assert len(detail.phase_timeline) == 2
        assert detail.phase_timeline[0]["phase"] == "PREWARM_GCP"

    async def test_budget_contention_tracked(self, bridge):
        evt = _make_event("budget_acquire", {
            "category": "MODEL_LOAD",
            "name": "prime",
            "wait_s": 3.2,
            "queue_depth": 1,
            "hard_slot": True,
        })
        await bridge.consume(evt)
        evt2 = _make_event("budget_release", {
            "category": "MODEL_LOAD",
            "name": "prime",
            "held_s": 12.3,
        })
        await bridge.consume(evt2)
        detail = bridge.detail_snapshot
        assert len(detail.budget_entries) >= 1

    async def test_handoff_trace_recorded(self, bridge):
        evt = _make_event("authority_transition", {
            "from_state": "BOOT_POLICY_ACTIVE",
            "to_state": "HANDOFF_PENDING",
            "guards_checked": 3,
            "duration_ms": 12,
        })
        await bridge.consume(evt)
        detail = bridge.detail_snapshot
        assert len(detail.handoff_trace) == 1
```

**Step 2: Run tests, verify fail**

**Step 3: Implement**

Create `backend/core/supervisor_tui_bridge.py` (~150 lines):
- `TUIBridge(EventConsumer)` with `consume()` that updates internal state.
- `InlineSummary` dataclass: `last_resolved_phase`, `authority_state`, `budget_active`, `budget_total`, `budget_hard_used`, `lease_status`, `lease_ttl_remaining`, `invariants_pass_count`, `invariants_total`.
- `DetailSnapshot` dataclass: `phase_timeline` (list of dicts), `budget_entries` (list), `lease_history` (list), `handoff_trace` (list), `invariant_results` (list).
- Properties `inline_summary` and `detail_snapshot` return current state.

Modify `backend/core/supervisor_tui.py` (~80 lines):
- Import `TUIBridge`, `InlineSummary`.
- Add a `StartupSequencingWidget` (Textual `Static` widget) that reads from `TUIBridge.inline_summary` and renders the compact summary.
- Add a `StartupDetailPanel` (Textual `Static` widget) for post-boot drill-down.
- Wire into the existing dashboard layout.

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/supervisor_tui_bridge.py tests/unit/core/test_startup_tui_widgets.py
git commit -m "feat(disease10): add TUI dashboard bridge for startup sequencing (Task 8)"
```

Note: The actual Textual widget changes to `supervisor_tui.py` are deferred to the integration phase to avoid breaking the existing TUI without end-to-end testing.

---

### Task 9: Integration Tests — Wiring & Recovery

**Files:**
- Create: `tests/integration/test_disease10_wiring.py`
- Create: `tests/integration/test_disease10_recovery.py`

**Context:** End-to-end tests composing all Disease 10 wiring modules. Uses `FakeProber` (no real GCP). Tests full boot sequence through handoff, and all three recovery scenarios.

**Step 1: Write the wiring tests**

```python
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
```

**Step 2: Write the recovery tests**

```python
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


class TestScenarioA_LeassLossDuringBoot:
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


class TestScenarioB_LeassLossPostHandoff:
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
```

**Step 3: Run tests, verify fail, then implement (orchestrator must be done first in Task 7)**

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add tests/integration/test_disease10_wiring.py tests/integration/test_disease10_recovery.py
git commit -m "test(disease10): add integration tests for wiring and recovery sequences (Task 9)"
```

---

### Task 10: Acceptance Tests — Full Boot Scenarios

**Files:**
- Create: `tests/integration/test_disease10_wiring_acceptance.py`

**Context:** High-level acceptance tests validating the go/no-go criteria from the original Disease 10 plan: deterministic boot with/without GCP, no routing oscillation, no Reactor spawn failures under budget, full causal trace for every degradation decision.

**Step 1: Write the acceptance tests**

```python
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

        # Lease failure event should have failure class
        lease_events = [e for e in events if e.event_type == "lease_probe"]
        assert any(
            e.detail.get("failure_class") is not None
            for e in lease_events
        )


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
```

**Step 2: Run tests, verify fail (need Task 7 first)**

**Step 3: These are pure test files — no implementation needed beyond Task 7**

**Step 4: Run all tests, verify pass**

```bash
python3 -m pytest tests/unit/core/test_startup_config.py \
                  tests/unit/core/test_startup_telemetry.py \
                  tests/unit/core/test_routing_authority_fsm.py \
                  tests/unit/core/test_startup_budget_policy.py \
                  tests/unit/core/test_gcp_vm_readiness_prober.py \
                  tests/unit/core/test_prime_router_mirror.py \
                  tests/unit/core/test_startup_orchestrator.py \
                  tests/unit/core/test_startup_tui_widgets.py \
                  tests/integration/test_disease10_wiring.py \
                  tests/integration/test_disease10_recovery.py \
                  tests/integration/test_disease10_wiring_acceptance.py -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add tests/integration/test_disease10_wiring_acceptance.py
git commit -m "test(disease10): add acceptance tests for go/no-go criteria (Task 10)"
```

---

## Acceptance Test Matrix

| Go/No-Go Criterion | Test Class | Validated |
|---------------------|-----------|-----------|
| Deterministic boot WITH GCP | `TestGoNoGo_DeterministicBootWithGCP` | Routes to GCP_PRIME, handoff completes |
| Deterministic boot WITHOUT GCP | `TestGoNoGo_DeterministicBootWithoutGCP` | Falls to LOCAL_MINIMAL |
| No routing oscillation | `TestGoNoGo_NoRoutingOscillation` | Single authority, monotonic progression |
| No Reactor spawn failures | `TestGoNoGo_NoReactorSpawnFailures` | Serialized behind model load |
| Full causal trace | `TestGoNoGo_CausalTrace` | Every event has trace_id + detail |
| Health responsive | `TestGoNoGo_HealthResponsive` | Queries work during budget contention |
| Lease loss during boot | `TestScenarioA_LeassLossDuringBoot` | Falls back, stays in boot authority |
| Lease loss post-handoff | `TestScenarioB_LeassLossPostHandoff` | Rolls back to boot authority |
| Handoff failure | `TestScenarioC_HandoffFailure` | Stays in boot authority |

## Estimated Test Count

| File | Tests |
|------|-------|
| test_startup_config.py | ~18 |
| test_startup_telemetry.py | ~12 |
| test_routing_authority_fsm.py | ~18 |
| test_startup_budget_policy.py | ~12 |
| test_gcp_vm_readiness_prober.py | ~8 |
| test_prime_router_mirror.py | ~7 |
| test_startup_orchestrator.py | ~14 |
| test_startup_tui_widgets.py | ~8 |
| test_disease10_wiring.py | ~6 |
| test_disease10_recovery.py | ~3 |
| test_disease10_wiring_acceptance.py | ~8 |
| **Total** | **~114** |
