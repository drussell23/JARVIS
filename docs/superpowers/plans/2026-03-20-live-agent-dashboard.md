# Live Agent Dashboard (TUI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Textual TUI dashboard with 4 tabs (Pipeline, Agents, System, Faults) + status bar that renders real telemetry envelopes from the TelemetryBus, giving a live view of JARVIS system state instead of a wall of logs.

**Architecture:** A `JarvisDashboard` Textual App runs in a daemon thread. A `TelemetryBusConsumer` subscribes to `"*"` on the TelemetryBus and routes each envelope to the appropriate panel by event schema domain. Each panel maintains its own bounded state and updates its Textual widgets via `call_from_thread()` for thread-safe rendering.

**Tech Stack:** Python 3.12, textual 8.0.0, asyncio, threading, TelemetryBus (Phase A)

**Spec:** `docs/superpowers/specs/2026-03-20-live-agent-dashboard-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/tui/__init__.py` | **NEW** — Package marker |
| `backend/core/tui/app.py` | **NEW** — JarvisDashboard App, start_dashboard(), StatusBar |
| `backend/core/tui/bus_consumer.py` | **NEW** — Routes envelopes to panels by domain |
| `backend/core/tui/pipeline_panel.py` | **NEW** — Command trace log (reasoning.decision events) |
| `backend/core/tui/agents_panel.py` | **NEW** — Agent inventory grid (scheduler events) |
| `backend/core/tui/system_panel.py` | **NEW** — Lifecycle + gate + bus stats (lifecycle events) |
| `backend/core/tui/faults_panel.py` | **NEW** — Active/resolved faults (fault events) |
| `tests/core/test_tui_panels.py` | **NEW** — Data layer tests for all panels |
| `unified_supervisor.py` | **MODIFY** — Zone 6.57: start_dashboard() (~5 lines) |

---

### Task 1: Panel Data Models (shared state types)

**Files:**
- Create: `backend/core/tui/__init__.py`
- Create: `backend/core/tui/pipeline_panel.py`
- Create: `backend/core/tui/agents_panel.py`
- Create: `backend/core/tui/system_panel.py`
- Create: `backend/core/tui/faults_panel.py`
- Create: `tests/core/test_tui_panels.py`

Each panel has a data model (state class) and an `update(envelope)` method. This task creates the data models and tests them WITHOUT any Textual widgets — pure data layer.

- [ ] **Step 1: Write failing tests**

```python
# tests/core/test_tui_panels.py
"""Tests for TUI dashboard panel data layers."""
import time
import pytest
from backend.core.telemetry_contract import TelemetryEnvelope
from backend.core.tui.pipeline_panel import PipelineData, CommandTrace
from backend.core.tui.agents_panel import AgentsData, AgentEntry
from backend.core.tui.system_panel import SystemData
from backend.core.tui.faults_panel import FaultsData, FaultEntry


def _make_envelope(schema: str, payload: dict, trace_id: str = "t1", source: str = "test") -> TelemetryEnvelope:
    return TelemetryEnvelope.create(
        event_schema=schema,
        source=source,
        trace_id=trace_id,
        span_id="s1",
        partition_key=schema.split(".")[0],
        payload=payload,
    )


class TestPipelineData:
    def test_new_command_creates_trace(self):
        data = PipelineData()
        env = _make_envelope("reasoning.decision@1.0.0", {
            "command": "start my day",
            "is_proactive": True,
            "confidence": 0.92,
            "signals": ["workflow_trigger"],
            "phase": "full_enable",
            "expanded_intents": ["check email", "check calendar"],
            "mind_requests": 2,
            "delegations": 2,
            "total_ms": 2300.0,
            "success_rate": 1.0,
        }, trace_id="abc-123")
        data.update(env)
        assert len(data.commands) == 1
        assert data.commands[0].trace_id == "abc-123"
        assert data.commands[0].command == "start my day"
        assert data.commands[0].is_proactive is True

    def test_bounded_at_50_commands(self):
        data = PipelineData(max_commands=50)
        for i in range(60):
            env = _make_envelope("reasoning.decision@1.0.0", {
                "command": f"cmd {i}", "is_proactive": False, "confidence": 0.1,
                "signals": [], "phase": "shadow", "expanded_intents": [],
                "mind_requests": 0, "delegations": 0, "total_ms": 100.0, "success_rate": 0.0,
            }, trace_id=f"t-{i}")
            data.update(env)
        assert len(data.commands) == 50

    def test_passthrough_command(self):
        data = PipelineData()
        env = _make_envelope("reasoning.decision@1.0.0", {
            "command": "what time is it",
            "is_proactive": False,
            "confidence": 0.1,
            "signals": [],
            "phase": "full_enable",
            "expanded_intents": [],
            "mind_requests": 0,
            "delegations": 0,
            "total_ms": 50.0,
            "success_rate": 0.0,
        }, trace_id="simple-1")
        data.update(env)
        assert data.commands[0].is_proactive is False
        assert data.commands[0].expanded_intents == []

    def test_command_count(self):
        data = PipelineData()
        data.update(_make_envelope("reasoning.decision@1.0.0", {
            "command": "a", "is_proactive": False, "confidence": 0.0,
            "signals": [], "phase": "shadow", "expanded_intents": [],
            "mind_requests": 0, "delegations": 0, "total_ms": 0, "success_rate": 0,
        }, trace_id="t1"))
        assert data.total_commands == 1


class TestAgentsData:
    def test_graph_state_populates_agents(self):
        data = AgentsData()
        env = _make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 3,
            "initialized": 3,
            "failed": 0,
            "agent_names": ["coordinator_agent", "predictive_planner", "memory_agent"],
        })
        data.update(env)
        assert len(data.agents) == 3
        assert "coordinator_agent" in data.agents

    def test_unit_state_updates_agent(self):
        data = AgentsData()
        # First populate
        data.update(_make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 1, "initialized": 1, "failed": 0,
            "agent_names": ["coordinator_agent"],
        }))
        # Then update state
        data.update(_make_envelope("scheduler.unit_state@1.0.0", {
            "agent_name": "coordinator_agent",
            "state": "busy",
            "tasks_completed": 5,
        }))
        assert data.agents["coordinator_agent"].state == "busy"

    def test_initialized_count(self):
        data = AgentsData()
        data.update(_make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 15, "initialized": 13, "failed": 2,
            "agent_names": ["a"] * 13,
        }))
        assert data.total_agents == 15
        assert data.initialized == 13


class TestSystemData:
    def test_lifecycle_transition_updates_state(self):
        data = SystemData()
        env = _make_envelope("lifecycle.transition@1.0.0", {
            "from_state": "PROBING",
            "to_state": "READY",
            "trigger": "health_check",
            "reason_code": "ready_for_inference",
            "attempt": 0,
            "restarts_in_window": 0,
            "elapsed_in_prev_state_ms": 5000.0,
        }, source="jprime_lifecycle_controller")
        data.update(env)
        assert data.lifecycle_state == "READY"

    def test_gate_activation_updates_state(self):
        data = SystemData()
        env = _make_envelope("reasoning.activation@1.0.0", {
            "from_state": "READY",
            "to_state": "ACTIVE",
            "trigger": "dwell_complete",
            "cause_code": "ACTIVATION_ARMED",
            "critical_deps": {
                "jprime_lifecycle": "HEALTHY",
                "coordinator_agent": "HEALTHY",
                "predictive_planner": "HEALTHY",
                "proactive_detector": "HEALTHY",
            },
            "gate_sequence": 3,
            "dwell_ms": 5000.0,
            "in_flight_preempted": 0,
            "degraded_overrides": {},
        })
        data.update(env)
        assert data.gate_state == "ACTIVE"

    def test_recent_transitions_bounded(self):
        data = SystemData()
        for i in range(25):
            data.update(_make_envelope("lifecycle.transition@1.0.0", {
                "from_state": "A", "to_state": "B", "trigger": "t",
                "reason_code": "r", "attempt": 0, "restarts_in_window": 0,
                "elapsed_in_prev_state_ms": 0,
            }))
        assert len(data.recent_transitions) <= 20


class TestFaultsData:
    def test_fault_raised(self):
        data = FaultsData()
        env = _make_envelope("fault.raised@1.0.0", {
            "fault_class": "connection_refused",
            "component": "jprime_lifecycle",
            "message": "J-Prime unreachable",
            "recovery_policy": "auto_restart",
            "terminal": False,
        }, trace_id="fault-1")
        data.update(env)
        assert len(data.active_faults) == 1
        assert data.active_faults[0].fault_class == "connection_refused"

    def test_fault_resolved(self):
        data = FaultsData()
        # Raise
        raise_env = _make_envelope("fault.raised@1.0.0", {
            "fault_class": "timeout",
            "component": "coordinator_agent",
            "message": "Agent timeout",
            "recovery_policy": "auto_restart",
            "terminal": False,
        }, trace_id="fault-2")
        data.update(raise_env)
        assert len(data.active_faults) == 1

        # Resolve
        resolve_env = _make_envelope("fault.resolved@1.0.0", {
            "fault_id": raise_env.event_id,
            "resolution": "auto_recovered",
            "duration_ms": 12000.0,
        }, trace_id="fault-2")
        data.update(resolve_env)
        assert len(data.active_faults) == 0
        assert len(data.resolved_faults) == 1

    def test_resolved_bounded(self):
        data = FaultsData()
        for i in range(25):
            env = _make_envelope("fault.raised@1.0.0", {
                "fault_class": "test", "component": "test",
                "message": "test", "recovery_policy": "none", "terminal": False,
            }, trace_id=f"f-{i}")
            data.update(env)
            data.update(_make_envelope("fault.resolved@1.0.0", {
                "fault_id": env.event_id, "resolution": "ok", "duration_ms": 100,
            }, trace_id=f"f-{i}"))
        assert len(data.resolved_faults) <= 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_tui_panels.py -v 2>&1 | head -20`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Create package and implement panel data models**

Create `backend/core/tui/__init__.py` (empty file).

Create `backend/core/tui/pipeline_panel.py`:

```python
"""Pipeline panel — command trace log data layer."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional
from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class CommandTrace:
    trace_id: str
    command: str
    is_proactive: bool
    confidence: float
    signals: List[str]
    phase: str
    expanded_intents: List[str]
    mind_requests: int
    delegations: int
    total_ms: float
    success_rate: float
    timestamp: float = 0.0


class PipelineData:
    """Data layer for the pipeline panel. No Textual dependency."""

    def __init__(self, max_commands: int = 50):
        self.commands: Deque[CommandTrace] = deque(maxlen=max_commands)
        self.total_commands: int = 0

    def update(self, envelope: TelemetryEnvelope) -> None:
        if envelope.event_schema.startswith("reasoning.decision"):
            p = envelope.payload
            trace = CommandTrace(
                trace_id=envelope.trace_id,
                command=p.get("command", ""),
                is_proactive=p.get("is_proactive", False),
                confidence=p.get("confidence", 0.0),
                signals=p.get("signals", []),
                phase=p.get("phase", ""),
                expanded_intents=p.get("expanded_intents", []),
                mind_requests=p.get("mind_requests", 0),
                delegations=p.get("delegations", 0),
                total_ms=p.get("total_ms", 0.0),
                success_rate=p.get("success_rate", 0.0),
                timestamp=envelope.emitted_at,
            )
            self.commands.append(trace)
            self.total_commands += 1
```

Create `backend/core/tui/agents_panel.py`:

```python
"""Agents panel — agent inventory data layer."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class AgentEntry:
    name: str
    state: str = "unknown"
    tasks_completed: int = 0
    errors: int = 0


class AgentsData:
    """Data layer for the agents panel."""

    def __init__(self):
        self.agents: Dict[str, AgentEntry] = {}
        self.total_agents: int = 0
        self.initialized: int = 0
        self.failed: int = 0

    def update(self, envelope: TelemetryEnvelope) -> None:
        p = envelope.payload
        if envelope.event_schema.startswith("scheduler.graph_state"):
            self.total_agents = p.get("total_agents", 0)
            self.initialized = p.get("initialized", 0)
            self.failed = p.get("failed", 0)
            for name in p.get("agent_names", []):
                if name not in self.agents:
                    self.agents[name] = AgentEntry(name=name, state="idle")
        elif envelope.event_schema.startswith("scheduler.unit_state"):
            name = p.get("agent_name", "")
            if name in self.agents:
                self.agents[name].state = p.get("state", "unknown")
                self.agents[name].tasks_completed = p.get("tasks_completed", self.agents[name].tasks_completed)
```

Create `backend/core/tui/system_panel.py`:

```python
"""System panel — lifecycle, gate, and bus stats data layer."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional
from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class TransitionEntry:
    timestamp: float
    domain: str  # "lifecycle" or "gate"
    from_state: str
    to_state: str
    trigger: str


class SystemData:
    """Data layer for the system panel."""

    def __init__(self):
        self.lifecycle_state: str = "UNKNOWN"
        self.lifecycle_restarts: int = 0
        self.gate_state: str = "DISABLED"
        self.gate_sequence: int = 0
        self.gate_deps: Dict[str, str] = {}
        self.recent_transitions: Deque[TransitionEntry] = deque(maxlen=20)

    def update(self, envelope: TelemetryEnvelope) -> None:
        p = envelope.payload
        if envelope.event_schema.startswith("lifecycle.transition"):
            self.lifecycle_state = p.get("to_state", self.lifecycle_state)
            self.lifecycle_restarts = p.get("restarts_in_window", self.lifecycle_restarts)
            self.recent_transitions.append(TransitionEntry(
                timestamp=envelope.emitted_at,
                domain="lifecycle",
                from_state=p.get("from_state", "?"),
                to_state=p.get("to_state", "?"),
                trigger=p.get("trigger", "?"),
            ))
        elif envelope.event_schema.startswith("reasoning.activation"):
            self.gate_state = p.get("to_state", self.gate_state)
            self.gate_sequence = p.get("gate_sequence", self.gate_sequence)
            self.gate_deps = p.get("critical_deps", self.gate_deps)
            self.recent_transitions.append(TransitionEntry(
                timestamp=envelope.emitted_at,
                domain="gate",
                from_state=p.get("from_state", "?"),
                to_state=p.get("to_state", "?"),
                trigger=p.get("trigger", "?"),
            ))
```

Create `backend/core/tui/faults_panel.py`:

```python
"""Faults panel — active and resolved faults data layer."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional
from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class FaultEntry:
    event_id: str
    fault_class: str
    component: str
    message: str
    recovery_policy: str
    terminal: bool
    timestamp: float
    resolved: bool = False
    resolution: str = ""
    duration_ms: float = 0.0


class FaultsData:
    """Data layer for the faults panel."""

    def __init__(self):
        self.active_faults: List[FaultEntry] = []
        self.resolved_faults: Deque[FaultEntry] = deque(maxlen=20)

    def update(self, envelope: TelemetryEnvelope) -> None:
        p = envelope.payload
        if envelope.event_schema.startswith("fault.raised"):
            self.active_faults.append(FaultEntry(
                event_id=envelope.event_id,
                fault_class=p.get("fault_class", ""),
                component=p.get("component", ""),
                message=p.get("message", ""),
                recovery_policy=p.get("recovery_policy", ""),
                terminal=p.get("terminal", False),
                timestamp=envelope.emitted_at,
            ))
        elif envelope.event_schema.startswith("fault.resolved"):
            fault_id = p.get("fault_id", "")
            for i, f in enumerate(self.active_faults):
                if f.event_id == fault_id:
                    f.resolved = True
                    f.resolution = p.get("resolution", "")
                    f.duration_ms = p.get("duration_ms", 0.0)
                    self.resolved_faults.append(f)
                    self.active_faults.pop(i)
                    break
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_tui_panels.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/tui/ tests/core/test_tui_panels.py
git commit -m "feat(tui): add panel data models for pipeline, agents, system, faults"
```

---

### Task 2: BusConsumer and StatusBar Data

**Files:**
- Create: `backend/core/tui/bus_consumer.py`
- Test: `tests/core/test_tui_panels.py` (append)

- [ ] **Step 1: Write failing tests**

APPEND to `tests/core/test_tui_panels.py`:

```python
from backend.core.tui.bus_consumer import TelemetryBusConsumer, StatusBarData


class TestBusConsumer:
    def test_routes_reasoning_to_pipeline(self):
        pipeline = PipelineData()
        agents = AgentsData()
        system = SystemData()
        faults = FaultsData()
        status = StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)

        env = _make_envelope("reasoning.decision@1.0.0", {
            "command": "test", "is_proactive": False, "confidence": 0.1,
            "signals": [], "phase": "shadow", "expanded_intents": [],
            "mind_requests": 0, "delegations": 0, "total_ms": 50, "success_rate": 0,
        })
        consumer.handle_sync(env)
        assert len(pipeline.commands) == 1

    def test_routes_lifecycle_to_system(self):
        pipeline = PipelineData()
        agents = AgentsData()
        system = SystemData()
        faults = FaultsData()
        status = StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)

        env = _make_envelope("lifecycle.transition@1.0.0", {
            "from_state": "UNKNOWN", "to_state": "READY", "trigger": "t",
            "reason_code": "r", "attempt": 0, "restarts_in_window": 0,
            "elapsed_in_prev_state_ms": 0,
        })
        consumer.handle_sync(env)
        assert system.lifecycle_state == "READY"

    def test_routes_scheduler_to_agents(self):
        pipeline = PipelineData()
        agents = AgentsData()
        system = SystemData()
        faults = FaultsData()
        status = StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)

        env = _make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 5, "initialized": 5, "failed": 0,
            "agent_names": ["a", "b", "c", "d", "e"],
        })
        consumer.handle_sync(env)
        assert agents.total_agents == 5

    def test_routes_fault_to_faults(self):
        pipeline = PipelineData()
        agents = AgentsData()
        system = SystemData()
        faults = FaultsData()
        status = StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)

        env = _make_envelope("fault.raised@1.0.0", {
            "fault_class": "test", "component": "test",
            "message": "test", "recovery_policy": "none", "terminal": False,
        })
        consumer.handle_sync(env)
        assert len(faults.active_faults) == 1


class TestStatusBarData:
    def test_updates_from_lifecycle(self):
        status = StatusBarData()
        env = _make_envelope("lifecycle.transition@1.0.0", {
            "to_state": "READY", "from_state": "PROBING",
            "trigger": "t", "reason_code": "r", "attempt": 0,
            "restarts_in_window": 0, "elapsed_in_prev_state_ms": 0,
        })
        status.update(env)
        assert status.lifecycle_state == "READY"

    def test_updates_from_gate(self):
        status = StatusBarData()
        env = _make_envelope("reasoning.activation@1.0.0", {
            "to_state": "ACTIVE", "from_state": "READY",
            "trigger": "t", "cause_code": "c", "critical_deps": {},
            "gate_sequence": 1, "dwell_ms": 0, "in_flight_preempted": 0,
            "degraded_overrides": {},
        })
        status.update(env)
        assert status.gate_state == "ACTIVE"

    def test_command_count_increments(self):
        status = StatusBarData()
        env = _make_envelope("reasoning.decision@1.0.0", {
            "command": "test", "is_proactive": False, "confidence": 0,
            "signals": [], "phase": "", "expanded_intents": [],
            "mind_requests": 0, "delegations": 0, "total_ms": 0, "success_rate": 0,
        })
        status.update(env)
        assert status.command_count == 1

    def test_to_string(self):
        status = StatusBarData()
        s = status.to_string()
        assert "J-Prime:" in s
        assert "Gate:" in s
        assert "Agents:" in s
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python3 -m pytest tests/core/test_tui_panels.py::TestBusConsumer -v 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement BusConsumer and StatusBarData**

```python
# backend/core/tui/bus_consumer.py
"""Routes TelemetryEnvelopes to dashboard panels by event schema domain."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from backend.core.telemetry_contract import TelemetryEnvelope
from backend.core.tui.pipeline_panel import PipelineData
from backend.core.tui.agents_panel import AgentsData
from backend.core.tui.system_panel import SystemData
from backend.core.tui.faults_panel import FaultsData


@dataclass
class StatusBarData:
    """One-line summary state, updated on every envelope."""
    lifecycle_state: str = "UNKNOWN"
    gate_state: str = "DISABLED"
    agent_count: str = "?/?"
    fault_count: int = 0
    command_count: int = 0
    bus_emitted: int = 0

    def update(self, envelope: TelemetryEnvelope) -> None:
        p = envelope.payload
        if envelope.event_schema.startswith("lifecycle.transition"):
            self.lifecycle_state = p.get("to_state", self.lifecycle_state)
        elif envelope.event_schema.startswith("reasoning.activation"):
            self.gate_state = p.get("to_state", self.gate_state)
        elif envelope.event_schema.startswith("scheduler.graph_state"):
            init = p.get("initialized", 0)
            total = p.get("total_agents", 0)
            self.agent_count = f"{init}/{total}"
        elif envelope.event_schema.startswith("reasoning.decision"):
            self.command_count += 1
        elif envelope.event_schema.startswith("fault.raised"):
            self.fault_count += 1
        elif envelope.event_schema.startswith("fault.resolved"):
            self.fault_count = max(0, self.fault_count - 1)
        self.bus_emitted += 1

    def to_string(self) -> str:
        return (
            f"J-Prime:{self.lifecycle_state} | Gate:{self.gate_state} | "
            f"Agents:{self.agent_count} | Faults:{self.fault_count} | "
            f"Cmds:{self.command_count} | Bus:{self.bus_emitted}"
        )


class TelemetryBusConsumer:
    """Routes envelopes to the correct panel by event schema domain."""

    def __init__(
        self,
        pipeline: PipelineData,
        agents: AgentsData,
        system: SystemData,
        faults: FaultsData,
        status: StatusBarData,
    ):
        self._pipeline = pipeline
        self._agents = agents
        self._system = system
        self._faults = faults
        self._status = status
        self._routing = {
            "reasoning": [self._pipeline, self._system],  # system gets gate events too
            "lifecycle": [self._system],
            "scheduler": [self._agents],
            "fault": [self._faults],
            "recovery": [self._faults],
        }

    def handle_sync(self, envelope: TelemetryEnvelope) -> None:
        """Synchronous handler — routes envelope to panels."""
        self._status.update(envelope)
        schema_domain = envelope.event_schema.split(".")[0]
        panels = self._routing.get(schema_domain, [])
        for panel in panels:
            panel.update(envelope)

    async def handle(self, envelope: TelemetryEnvelope) -> None:
        """Async handler for TelemetryBus subscription."""
        self.handle_sync(envelope)
```

- [ ] **Step 4: Run ALL tests**

Run: `python3 -m pytest tests/core/test_tui_panels.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/tui/bus_consumer.py tests/core/test_tui_panels.py
git commit -m "feat(tui): add BusConsumer routing and StatusBarData"
```

---

### Task 3: Textual App — JarvisDashboard with Panels

**Files:**
- Create: `backend/core/tui/app.py`
- Test: `tests/core/test_tui_panels.py` (append)

This task creates the actual Textual App with 4 tabs and a status bar. Each tab contains a `RichLog` widget that renders from the panel data model.

- [ ] **Step 1: Write failing test**

APPEND to `tests/core/test_tui_panels.py`:

```python
class TestDashboardImport:
    def test_app_importable(self):
        from backend.core.tui.app import JarvisDashboard, start_dashboard
        assert JarvisDashboard is not None
        assert callable(start_dashboard)

    def test_app_creates_without_error(self):
        from backend.core.tui.app import JarvisDashboard
        app = JarvisDashboard()
        assert app is not None
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/core/test_tui_panels.py::TestDashboardImport -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement JarvisDashboard**

```python
# backend/core/tui/app.py
"""JARVIS Live Agent Dashboard — Textual TUI Application.

Replaces the wall-of-logs experience with a structured, tabbed,
real-time dashboard. Consumes TelemetryEnvelopes only — never
imports supervisor or processor internals.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Static, TabbedContent, TabPane, RichLog

from backend.core.tui.pipeline_panel import PipelineData, CommandTrace
from backend.core.tui.agents_panel import AgentsData
from backend.core.tui.system_panel import SystemData
from backend.core.tui.faults_panel import FaultsData
from backend.core.tui.bus_consumer import TelemetryBusConsumer, StatusBarData

logger = logging.getLogger(__name__)


class StatusBar(Static):
    """Always-visible one-line status summary."""

    def __init__(self, data: StatusBarData, **kwargs):
        super().__init__("", **kwargs)
        self._data = data

    def refresh_display(self) -> None:
        self.update(self._data.to_string())


class JarvisDashboard(App):
    """JARVIS Live Agent Dashboard."""

    TITLE = "JARVIS Dashboard"
    CSS = """
    Screen {
        background: $surface;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pipeline_data = PipelineData()
        self.agents_data = AgentsData()
        self.system_data = SystemData()
        self.faults_data = FaultsData()
        self.status_data = StatusBarData()
        self.consumer = TelemetryBusConsumer(
            self.pipeline_data,
            self.agents_data,
            self.system_data,
            self.faults_data,
            self.status_data,
        )
        self._pipeline_log: Optional[RichLog] = None
        self._agents_log: Optional[RichLog] = None
        self._system_log: Optional[RichLog] = None
        self._faults_log: Optional[RichLog] = None
        self._status_bar: Optional[StatusBar] = None
        self._last_pipeline_count = 0
        self._last_system_count = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            with TabPane("Pipeline", id="pipeline"):
                yield RichLog(id="pipeline-log", highlight=True, markup=True)
            with TabPane("Agents", id="agents"):
                yield RichLog(id="agents-log", highlight=True, markup=True)
            with TabPane("System", id="system"):
                yield RichLog(id="system-log", highlight=True, markup=True)
            with TabPane("Faults", id="faults"):
                yield RichLog(id="faults-log", highlight=True, markup=True)
        yield StatusBar(self.status_data, id="status-bar")

    def on_mount(self) -> None:
        self._pipeline_log = self.query_one("#pipeline-log", RichLog)
        self._agents_log = self.query_one("#agents-log", RichLog)
        self._system_log = self.query_one("#system-log", RichLog)
        self._faults_log = self.query_one("#faults-log", RichLog)
        self._status_bar = self.query_one("#status-bar", StatusBar)
        # Refresh every second
        self.set_interval(1.0, self._refresh_panels)

    def _refresh_panels(self) -> None:
        """Periodic refresh — re-render panels from data models."""
        self._refresh_pipeline()
        self._refresh_agents()
        self._refresh_system()
        self._refresh_faults()
        if self._status_bar:
            self._status_bar.refresh_display()

    def _refresh_pipeline(self) -> None:
        if not self._pipeline_log:
            return
        count = self.pipeline_data.total_commands
        if count == self._last_pipeline_count:
            return
        self._last_pipeline_count = count
        log = self._pipeline_log
        log.clear()
        for cmd in self.pipeline_data.commands:
            ts = datetime.fromtimestamp(cmd.timestamp).strftime("%H:%M:%S") if cmd.timestamp else "??:??:??"
            if cmd.is_proactive:
                log.write(f"[bold green]{ts}[/] [white]\"{cmd.command}\"[/] trace={cmd.trace_id}")
                log.write(f"  DETECT   proactive=true  conf={cmd.confidence:.2f}  signals={cmd.signals}")
                if cmd.expanded_intents:
                    log.write(f"  EXPAND   {len(cmd.expanded_intents)} intents {cmd.expanded_intents}")
                if cmd.mind_requests:
                    log.write(f"  MIND     {cmd.mind_requests} requests")
                if cmd.delegations:
                    log.write(f"  COORD    {cmd.delegations} delegations")
                log.write(f"  [bold green]DONE[/]     success={cmd.success_rate:.0%}  total={cmd.total_ms:.0f}ms")
            else:
                log.write(f"[dim]{ts}[/] [white]\"{cmd.command}\"[/] -> passthrough ({cmd.total_ms:.0f}ms)")

    def _refresh_agents(self) -> None:
        if not self._agents_log:
            return
        log = self._agents_log
        log.clear()
        log.write(f"[bold]AGENTS ({self.agents_data.initialized}/{self.agents_data.total_agents} initialized)[/]")
        log.write("")
        from backend.core.reasoning_activation_gate import CRITICAL_FOR_REASONING
        critical_names = {"coordinator_agent", "predictive_planner"}
        # Critical first
        for name, agent in sorted(self.agents_data.agents.items()):
            if name in critical_names:
                color = "green" if agent.state == "idle" else "yellow" if agent.state == "busy" else "red"
                log.write(f"  [{color}]*[/] {name:<25} [{color}]{agent.state}[/]  tasks:{agent.tasks_completed}")
        # Non-critical
        for name, agent in sorted(self.agents_data.agents.items()):
            if name not in critical_names:
                color = "green" if agent.state == "idle" else "yellow" if agent.state == "busy" else "red"
                log.write(f"  [{color}]*[/] {name:<25} [{color}]{agent.state}[/]")

    def _refresh_system(self) -> None:
        if not self._system_log:
            return
        count = len(self.system_data.recent_transitions)
        if count == self._last_system_count and count > 0:
            return
        self._last_system_count = count
        log = self._system_log
        log.clear()
        # Lifecycle
        lc = self.system_data.lifecycle_state
        lc_color = "green" if lc == "READY" else "yellow" if lc == "DEGRADED" else "red"
        log.write(f"[bold]J-PRIME LIFECYCLE[/]")
        log.write(f"  State:    [{lc_color}]{lc}[/]")
        log.write(f"  Restarts: {self.system_data.lifecycle_restarts}")
        log.write("")
        # Gate
        gs = self.system_data.gate_state
        gs_color = "green" if gs == "ACTIVE" else "yellow" if gs == "DEGRADED" else "red" if gs in ("BLOCKED", "TERMINAL") else "dim"
        log.write(f"[bold]REASONING GATE[/]")
        log.write(f"  State:    [{gs_color}]{gs}[/]")
        log.write(f"  Sequence: {self.system_data.gate_sequence}")
        if self.system_data.gate_deps:
            deps_str = "  ".join(f"{k}={v}" for k, v in self.system_data.gate_deps.items())
            log.write(f"  Deps:     {deps_str}")
        log.write("")
        # Transitions
        log.write(f"[bold]RECENT TRANSITIONS[/]")
        for t in list(self.system_data.recent_transitions)[-10:]:
            ts = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
            log.write(f"  {ts}  {t.domain:<10} {t.from_state} -> {t.to_state}  ({t.trigger})")

    def _refresh_faults(self) -> None:
        if not self._faults_log:
            return
        log = self._faults_log
        log.clear()
        log.write(f"[bold]ACTIVE FAULTS ({len(self.faults_data.active_faults)})[/]")
        if not self.faults_data.active_faults:
            log.write("  [dim](none)[/]")
        for f in self.faults_data.active_faults:
            ts = datetime.fromtimestamp(f.timestamp).strftime("%H:%M:%S")
            log.write(f"  [red]{ts}[/]  {f.component}  {f.fault_class}  {f.message}")
        log.write("")
        log.write(f"[bold]RESOLVED ({len(self.faults_data.resolved_faults)})[/]")
        for f in list(self.faults_data.resolved_faults)[-10:]:
            ts = datetime.fromtimestamp(f.timestamp).strftime("%H:%M:%S")
            log.write(f"  [dim]{ts}[/]  {f.component}  {f.fault_class}  {f.resolution}  ({f.duration_ms:.0f}ms)")

    def on_envelope(self, envelope) -> None:
        """Thread-safe envelope ingestion from TelemetryBus."""
        self.consumer.handle_sync(envelope)


def start_dashboard() -> Optional[threading.Thread]:
    """Start the TUI dashboard in a daemon thread.

    Returns the thread if started, None if no terminal attached.
    """
    if not sys.stdout.isatty():
        logger.info("[TUI] No terminal attached — dashboard skipped")
        return None

    try:
        from backend.core.telemetry_contract import get_telemetry_bus

        app = JarvisDashboard()
        bus = get_telemetry_bus()

        async def bus_handler(envelope):
            app.consumer.handle_sync(envelope)

        bus.subscribe("*", bus_handler)

        thread = threading.Thread(
            target=app.run,
            name="jarvis-tui-dashboard",
            daemon=True,
        )
        thread.start()
        logger.info("[TUI] Dashboard started in daemon thread")
        return thread
    except Exception as exc:
        logger.warning("[TUI] Dashboard failed to start: %s", exc)
        return None
```

- [ ] **Step 4: Run ALL tests**

Run: `python3 -m pytest tests/core/test_tui_panels.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/tui/app.py tests/core/test_tui_panels.py
git commit -m "feat(tui): implement JarvisDashboard Textual app with 4 tabs and status bar"
```

---

### Task 4: Wire into Supervisor + Regression Check

**Files:**
- Modify: `unified_supervisor.py` (between Zone 6.56 and Zone 6.7)
- Test: `tests/core/test_tui_panels.py` (append)

- [ ] **Step 1: Write integration test**

APPEND to `tests/core/test_tui_panels.py`:

```python
class TestStartDashboard:
    def test_start_dashboard_returns_none_without_tty(self):
        from backend.core.tui.app import start_dashboard
        from unittest.mock import patch
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            result = start_dashboard()
        assert result is None
```

- [ ] **Step 2: Run test**

Run: `python3 -m pytest tests/core/test_tui_panels.py::TestStartDashboard -v`
Expected: PASS

- [ ] **Step 3: Wire into supervisor**

Find the area in `unified_supervisor.py` after Zone 6.56 (reasoning gate start) and before Zone 6.7 (AGI OS). Insert:

```python
            # =================================================================
            # v300.3: Zone 6.57 — Live Agent Dashboard (TUI)
            # Daemon thread, fault-isolated. Consumes TelemetryBus only.
            # =================================================================
            try:
                from backend.core.tui.app import start_dashboard
                _dashboard_thread = start_dashboard()
                if _dashboard_thread:
                    self.logger.info("[Kernel] Zone 6.57: TUI dashboard started")
                else:
                    self.logger.info("[Kernel] Zone 6.57: TUI dashboard skipped (no terminal)")
            except Exception as exc:
                self.logger.debug("[Kernel] Zone 6.57: TUI dashboard unavailable: %s", exc)
```

- [ ] **Step 4: Run all TUI tests**

Run: `python3 -m pytest tests/core/test_tui_panels.py -v`
Expected: All PASS

- [ ] **Step 5: Run full regression**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -q --tb=no --timeout=60 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 6: Verify imports**

Run: `python3 -c "from backend.core.tui.app import JarvisDashboard, start_dashboard; print('TUI imports OK')"`
Expected: `TUI imports OK`

- [ ] **Step 7: Commit**

```bash
git add unified_supervisor.py tests/core/test_tui_panels.py
git commit -m "feat(tui): wire dashboard into supervisor Zone 6.57"
```

- [ ] **Step 8: Final commit**

```bash
git add -A
git status
git commit -m "feat(tui): complete Live Agent Dashboard (Phase C)

Textual TUI with 4 tabs (Pipeline, Agents, System, Faults) +
status bar. Consumes real TelemetryEnvelopes from TelemetryBus.
Daemon thread, fault-isolated, no supervisor internals imported.

New: backend/core/tui/ (7 files)
Modified: unified_supervisor.py (Zone 6.57)"
```
