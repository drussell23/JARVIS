# Neural Mesh Agent Boot + Reasoning Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Initialize all 15 Neural Mesh agents at boot, then activate the reasoning chain through a capability-scoped 7-state ReasoningActivationGate that ensures critical dependencies (J-Prime, CoordinatorAgent, PredictivePlanningAgent, ProactiveCommandDetector) are healthy before reasoning processes commands.

**Architecture:** Agent initialization happens independently of J-Prime (Zone 6.55). A new `ReasoningActivationGate` (7-state FSM) polls critical dependencies, applies debounce/dwell rules, and controls whether the reasoning chain accepts commands. The gate emits `reasoning.activation@1.0.0` telemetry via the frozen contract. The existing `ReasoningChainOrchestrator.process()` checks the gate before processing.

**Tech Stack:** Python 3.12, asyncio, dataclasses, enum, TelemetryBus (Phase A), pytest

**Spec:** `docs/superpowers/specs/2026-03-20-neural-mesh-boot-reasoning-activation-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/reasoning_activation_gate.py` | **NEW** — GateState enum, GateConfig, DependencyHealth, ReasoningActivationGate, singleton |
| `tests/core/test_reasoning_activation_gate.py` | **NEW** — Gate transitions, dep health, preemption, flap suppression, failure injection |
| `backend/core/reasoning_chain_orchestrator.py` | **MODIFY** — Add gate check at top of `process()`, degraded override support |
| `unified_supervisor.py` | **MODIFY** — Zone 6.55: `initialize_all_agents()`. Zone 6.56: start gate. |

---

### Task 1: GateState Enum, GateConfig, and DependencyHealth

**Files:**
- Create: `backend/core/reasoning_activation_gate.py`
- Create: `tests/core/test_reasoning_activation_gate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/core/test_reasoning_activation_gate.py
"""Tests for ReasoningActivationGate."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.reasoning_activation_gate import (
    GateState,
    GateConfig,
    DepHealth,
    DepStatus,
    CRITICAL_FOR_REASONING,
)


class TestGateState:
    def test_all_states(self):
        states = [s.value for s in GateState]
        for expected in ["DISABLED", "WAITING_DEPS", "READY", "ACTIVE", "DEGRADED", "BLOCKED", "TERMINAL"]:
            assert expected in states

    def test_accepts_commands(self):
        assert GateState.ACTIVE.accepts_commands is True
        assert GateState.DEGRADED.accepts_commands is True
        assert GateState.DISABLED.accepts_commands is False
        assert GateState.WAITING_DEPS.accepts_commands is False
        assert GateState.READY.accepts_commands is False
        assert GateState.BLOCKED.accepts_commands is False
        assert GateState.TERMINAL.accepts_commands is False


class TestGateConfig:
    def test_defaults(self):
        c = GateConfig()
        assert c.activation_dwell_s == 5.0
        assert c.min_state_dwell_s == 3.0
        assert c.degrade_threshold == 3
        assert c.block_threshold == 3
        assert c.recovery_threshold == 3
        assert c.max_block_duration_s == 300.0
        assert c.terminal_cooldown_s == 900.0
        assert c.dep_poll_interval_s == 10.0

    def test_from_env(self):
        env = {
            "REASONING_ACTIVATION_DWELL_S": "10",
            "REASONING_DEGRADE_THRESHOLD": "5",
            "REASONING_DEP_POLL_S": "20",
        }
        with patch.dict("os.environ", env, clear=False):
            c = GateConfig.from_env()
        assert c.activation_dwell_s == 10.0
        assert c.degrade_threshold == 5
        assert c.dep_poll_interval_s == 20.0


class TestDepStatus:
    def test_status_values(self):
        assert DepStatus.HEALTHY.value == "HEALTHY"
        assert DepStatus.DEGRADED.value == "DEGRADED"
        assert DepStatus.UNAVAILABLE.value == "UNAVAILABLE"


class TestCriticalDeps:
    def test_critical_set(self):
        assert "jprime_lifecycle" in CRITICAL_FOR_REASONING
        assert "coordinator_agent" in CRITICAL_FOR_REASONING
        assert "predictive_planner" in CRITICAL_FOR_REASONING
        assert "proactive_detector" in CRITICAL_FOR_REASONING
        assert len(CRITICAL_FOR_REASONING) == 4
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py -v 2>&1 | head -20`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement data models**

```python
# backend/core/reasoning_activation_gate.py
"""
Reasoning Activation Gate
=========================

7-state FSM that controls whether the reasoning chain accepts commands.
Uses capability-scoped gating: reasoning activates only when critical
dependencies (J-Prime + specific agents) are healthy.

Non-critical agents run independently and are not gated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


CRITICAL_FOR_REASONING: Set[str] = {
    "jprime_lifecycle",
    "coordinator_agent",
    "predictive_planner",
    "proactive_detector",
}


class GateState(str, Enum):
    DISABLED = "DISABLED"
    WAITING_DEPS = "WAITING_DEPS"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    TERMINAL = "TERMINAL"

    @property
    def accepts_commands(self) -> bool:
        return self in (GateState.ACTIVE, GateState.DEGRADED)


class DepStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class DepHealth:
    """Health snapshot for one dependency."""
    name: str
    status: DepStatus
    last_check: float = 0.0
    response_time_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class GateConfig:
    activation_dwell_s: float = 5.0
    min_state_dwell_s: float = 3.0
    degrade_threshold: int = 3
    block_threshold: int = 3
    recovery_threshold: int = 3
    max_block_duration_s: float = 300.0
    terminal_cooldown_s: float = 900.0
    dep_poll_interval_s: float = 10.0

    @classmethod
    def from_env(cls) -> GateConfig:
        return cls(
            activation_dwell_s=_env_float("REASONING_ACTIVATION_DWELL_S", 5.0),
            min_state_dwell_s=_env_float("REASONING_MIN_DWELL_S", 3.0),
            degrade_threshold=_env_int("REASONING_DEGRADE_THRESHOLD", 3),
            block_threshold=_env_int("REASONING_BLOCK_THRESHOLD", 3),
            recovery_threshold=_env_int("REASONING_RECOVERY_THRESHOLD", 3),
            max_block_duration_s=_env_float("REASONING_MAX_BLOCK_S", 300.0),
            terminal_cooldown_s=_env_float("REASONING_TERMINAL_COOLDOWN_S", 900.0),
            dep_poll_interval_s=_env_float("REASONING_DEP_POLL_S", 10.0),
        )


# Degraded mode overrides (applied when gate is DEGRADED)
DEGRADED_OVERRIDES = {
    "proactive_threshold_boost": 0.1,     # +0.1 to proactive_threshold
    "auto_expand_threshold": 1.0,          # Never auto-expand in degraded
    "expansion_timeout_factor": 0.5,       # Half the expansion timeout
    "mind_request_timeout_factor": 0.5,    # Half the mind timeout
}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_activation_gate.py tests/core/test_reasoning_activation_gate.py
git commit -m "feat(gate): add GateState, GateConfig, DepHealth, and critical dep set"
```

---

### Task 2: ReasoningActivationGate — Core FSM

**Files:**
- Modify: `backend/core/reasoning_activation_gate.py`
- Test: `tests/core/test_reasoning_activation_gate.py`

This is the main task — the 7-state gate with transitions, dep polling, dwell/debounce, preemption, flap suppression, and telemetry.

- [ ] **Step 1: Write failing tests**

APPEND to `tests/core/test_reasoning_activation_gate.py`:

```python
from backend.core.reasoning_activation_gate import (
    ReasoningActivationGate,
    get_reasoning_activation_gate,
)


def _mock_dep_statuses(all_healthy=True, jprime="HEALTHY", coordinator="HEALTHY", planner="HEALTHY", detector="HEALTHY"):
    """Create mock dep health results."""
    return {
        "jprime_lifecycle": DepHealth("jprime_lifecycle", DepStatus(jprime)),
        "coordinator_agent": DepHealth("coordinator_agent", DepStatus(coordinator)),
        "predictive_planner": DepHealth("predictive_planner", DepStatus(planner)),
        "proactive_detector": DepHealth("proactive_detector", DepStatus(detector)),
    }


class TestGateTransitions:
    def _make_gate(self, **overrides):
        config = GateConfig(
            activation_dwell_s=0.01,
            min_state_dwell_s=0.01,
            dep_poll_interval_s=0.01,
            max_block_duration_s=1.0,
            terminal_cooldown_s=2.0,
        )
        gate = ReasoningActivationGate(config=config)
        gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses())
        for k, v in overrides.items():
            setattr(gate, k, v)
        return gate

    @pytest.mark.asyncio
    async def test_initial_state_disabled(self):
        gate = self._make_gate()
        assert gate.state == GateState.DISABLED

    @pytest.mark.asyncio
    async def test_flags_on_transitions_to_waiting(self):
        gate = self._make_gate()
        env = {"JARVIS_REASONING_CHAIN_ENABLED": "true"}
        with patch.dict("os.environ", env, clear=False):
            await gate._evaluate_flags()
        assert gate.state == GateState.WAITING_DEPS

    @pytest.mark.asyncio
    async def test_all_deps_healthy_transitions_to_ready(self):
        gate = self._make_gate()
        gate._state = GateState.WAITING_DEPS
        gate._state_entered_at = time.monotonic() - 1  # past dwell
        await gate._evaluate_deps()
        assert gate.state == GateState.READY

    @pytest.mark.asyncio
    async def test_ready_dwell_transitions_to_active(self):
        gate = self._make_gate()
        gate._state = GateState.READY
        gate._state_entered_at = time.monotonic() - 1  # past dwell
        await gate._evaluate_dwell()
        assert gate.state == GateState.ACTIVE

    @pytest.mark.asyncio
    async def test_active_accepts_commands(self):
        gate = self._make_gate()
        gate._state = GateState.ACTIVE
        assert gate.is_active() is True

    @pytest.mark.asyncio
    async def test_degraded_accepts_commands(self):
        gate = self._make_gate()
        gate._state = GateState.DEGRADED
        assert gate.is_active() is True

    @pytest.mark.asyncio
    async def test_blocked_rejects_commands(self):
        gate = self._make_gate()
        gate._state = GateState.BLOCKED
        assert gate.is_active() is False

    @pytest.mark.asyncio
    async def test_active_to_degraded_on_jprime_degraded(self):
        gate = self._make_gate()
        gate._state = GateState.ACTIVE
        gate._state_entered_at = time.monotonic() - 1
        gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses(jprime="DEGRADED"))
        for _ in range(3):
            await gate._evaluate_deps()
        assert gate.state == GateState.DEGRADED

    @pytest.mark.asyncio
    async def test_active_to_blocked_on_dep_unavailable(self):
        gate = self._make_gate()
        gate._state = GateState.ACTIVE
        gate._state_entered_at = time.monotonic() - 1
        gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses(coordinator="UNAVAILABLE"))
        for _ in range(3):
            await gate._evaluate_deps()
        assert gate.state == GateState.BLOCKED

    @pytest.mark.asyncio
    async def test_degraded_to_active_on_recovery(self):
        gate = self._make_gate()
        gate._state = GateState.DEGRADED
        gate._state_entered_at = time.monotonic() - 1
        gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses())
        for _ in range(3):
            await gate._evaluate_deps()
        assert gate.state == GateState.ACTIVE

    @pytest.mark.asyncio
    async def test_blocked_to_terminal_after_max_duration(self):
        gate = self._make_gate()
        gate._state = GateState.BLOCKED
        gate._state_entered_at = time.monotonic() - 2  # past max_block (1s)
        await gate._evaluate_block_duration()
        assert gate.state == GateState.TERMINAL

    @pytest.mark.asyncio
    async def test_terminal_auto_resets(self):
        gate = self._make_gate()
        gate._state = GateState.TERMINAL
        gate._state_entered_at = time.monotonic() - 3  # past cooldown (2s)
        await gate._evaluate_terminal_cooldown()
        assert gate.state == GateState.WAITING_DEPS


class TestGateFlapSuppression:
    @pytest.mark.asyncio
    async def test_rapid_transitions_suppressed(self):
        config = GateConfig(min_state_dwell_s=1.0, activation_dwell_s=0.01)
        gate = ReasoningActivationGate(config=config)
        gate._state = GateState.ACTIVE
        gate._state_entered_at = time.monotonic()  # just entered
        # Try to transition immediately — should be suppressed
        suppressed = await gate._try_transition(GateState.DEGRADED, "test", "test")
        assert suppressed is False
        assert gate.state == GateState.ACTIVE  # unchanged


class TestGateSequence:
    @pytest.mark.asyncio
    async def test_sequence_increments_on_transition(self):
        config = GateConfig(min_state_dwell_s=0.01, activation_dwell_s=0.01)
        gate = ReasoningActivationGate(config=config)
        seq_before = gate.gate_sequence
        gate._state_entered_at = time.monotonic() - 1
        await gate._try_transition(GateState.WAITING_DEPS, "test", "test")
        assert gate.gate_sequence == seq_before + 1


class TestGateTelemetry:
    @pytest.mark.asyncio
    async def test_transition_emits_envelope(self):
        config = GateConfig(min_state_dwell_s=0.01, activation_dwell_s=0.01)
        gate = ReasoningActivationGate(config=config)
        gate._state_entered_at = time.monotonic() - 1
        gate._dep_statuses = _mock_dep_statuses()

        from backend.core.telemetry_contract import TelemetryBus
        bus = TelemetryBus(max_queue=100)
        received = []
        async def handler(env): received.append(env)
        bus.subscribe("reasoning.*", handler)

        with patch("backend.core.reasoning_activation_gate.get_telemetry_bus", return_value=bus):
            await bus.start()
            await gate._try_transition(GateState.WAITING_DEPS, "flags_enabled", "FLAGS_ON")
            await asyncio.sleep(0.1)
            await bus.stop()

        assert len(received) == 1
        assert received[0].event_schema == "reasoning.activation@1.0.0"
        assert received[0].payload["to_state"] == "WAITING_DEPS"
        assert "critical_deps" in received[0].payload


class TestGateSingleton:
    def test_singleton(self):
        import backend.core.reasoning_activation_gate as mod
        mod._gate_instance = None
        g1 = get_reasoning_activation_gate()
        g2 = get_reasoning_activation_gate()
        assert g1 is g2
        mod._gate_instance = None
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py::TestGateTransitions -v 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement ReasoningActivationGate**

APPEND to `backend/core/reasoning_activation_gate.py`. The gate needs:

- `__init__()` — config, state=DISABLED, counters, dep_statuses dict, gate_sequence, polling task
- `state` property, `gate_sequence` property, `is_active()` method
- `_try_transition(to, trigger, cause_code)` — guarded by asyncio.Lock + dwell check + flap suppression + telemetry emission
- `_evaluate_flags()` — check env vars, DISABLED->WAITING_DEPS if flags on
- `_evaluate_deps()` — check dep health, evaluate WAITING_DEPS->READY, ACTIVE->DEGRADED, ACTIVE->BLOCKED, DEGRADED->ACTIVE, DEGRADED->BLOCKED
- `_evaluate_dwell()` — READY->ACTIVE after activation_dwell_s
- `_evaluate_block_duration()` — BLOCKED->TERMINAL after max_block
- `_evaluate_terminal_cooldown()` — TERMINAL->WAITING_DEPS after cooldown
- `_check_all_deps()` — probe each critical dep, return Dict[str, DepHealth]. Injectable for tests.
- `_check_jprime()` — read lifecycle controller state
- `_check_agent(name)` — call agent's get_stats() with timeout
- `start()` / `_poll_loop()` / `stop()` — background polling
- `get_degraded_config()` — returns overrides when DEGRADED
- Telemetry via `reasoning.activation@1.0.0` envelope on every transition

Key patterns:
- `_try_transition()` checks `time.monotonic() - self._state_entered_at >= self._config.min_state_dwell_s` before allowing transition (flap suppression)
- `_evaluate_deps()` tracks consecutive counters per-dep for threshold-based transitions
- Gate sequence is a monotonic int that increments on every transition (for orphan detection)
- `_check_all_deps()` is injectable (AsyncMock in tests) to avoid importing real agents

```python
class ReasoningActivationGate:
    """7-state FSM controlling reasoning chain activation."""

    def __init__(self, config: Optional[GateConfig] = None):
        self._config = config or GateConfig.from_env()
        self._state = GateState.DISABLED
        self._state_entered_at = time.monotonic()
        self._gate_sequence = 0
        self._lock = asyncio.Lock()
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

        # Dependency state
        self._dep_statuses: Dict[str, DepHealth] = {}
        self._consecutive_degraded = 0
        self._consecutive_failures = 0
        self._consecutive_healthy = 0

        # Transition log
        self._transitions: Deque[Dict[str, Any]] = deque(maxlen=100)

    @property
    def state(self) -> GateState:
        return self._state

    @property
    def gate_sequence(self) -> int:
        return self._gate_sequence

    def is_active(self) -> bool:
        return self._state.accepts_commands

    def get_degraded_config(self) -> Dict[str, Any]:
        if self._state == GateState.DEGRADED:
            return dict(DEGRADED_OVERRIDES)
        return {}

    async def _try_transition(self, to: GateState, trigger: str, cause_code: str) -> bool:
        """Attempt state transition with dwell guard and flap suppression."""
        async with self._lock:
            if self._state == to:
                return False
            elapsed = time.monotonic() - self._state_entered_at
            if elapsed < self._config.min_state_dwell_s:
                logger.debug("[Gate] Flap suppressed: %s->%s (dwell %.1fs < %.1fs)",
                    self._state.value, to.value, elapsed, self._config.min_state_dwell_s)
                return False

            prev = self._state
            self._state = to
            self._state_entered_at = time.monotonic()
            self._gate_sequence += 1

            # Reset counters on state entry
            if to == GateState.ACTIVE:
                self._consecutive_degraded = 0
                self._consecutive_failures = 0
            elif to in (GateState.WAITING_DEPS, GateState.DISABLED):
                self._consecutive_degraded = 0
                self._consecutive_failures = 0
                self._consecutive_healthy = 0

            logger.info("[Gate] %s -> %s (trigger=%s, cause=%s, seq=%d)",
                prev.value, to.value, trigger, cause_code, self._gate_sequence)

            # Emit telemetry
            self._emit_transition(prev, to, trigger, cause_code)
            return True

    def _emit_transition(self, from_state: GateState, to_state: GateState,
                         trigger: str, cause_code: str) -> None:
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope, get_telemetry_bus
            envelope = TelemetryEnvelope.create(
                event_schema="reasoning.activation@1.0.0",
                source="reasoning_activation_gate",
                trace_id="",
                span_id=str(uuid.uuid4())[:8],
                partition_key="reasoning",
                severity="warning" if to_state in (GateState.BLOCKED, GateState.TERMINAL) else "info",
                payload={
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                    "trigger": trigger,
                    "cause_code": cause_code,
                    "critical_deps": {
                        name: dep.status.value for name, dep in self._dep_statuses.items()
                    } if self._dep_statuses else {},
                    "gate_sequence": self._gate_sequence,
                    "dwell_ms": (time.monotonic() - self._state_entered_at) * 1000,
                    "in_flight_preempted": 0,
                    "degraded_overrides": dict(DEGRADED_OVERRIDES) if to_state == GateState.DEGRADED else {},
                },
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            pass

    async def _evaluate_flags(self) -> None:
        enabled = _env_bool("JARVIS_REASONING_CHAIN_ENABLED")
        shadow = _env_bool("JARVIS_REASONING_CHAIN_SHADOW")
        if (enabled or shadow) and self._state == GateState.DISABLED:
            await self._try_transition(GateState.WAITING_DEPS, "flags_enabled", "FLAGS_ON")
        elif not enabled and not shadow and self._state != GateState.DISABLED:
            await self._try_transition(GateState.DISABLED, "flags_disabled", "FLAGS_OFF")

    async def _check_all_deps(self) -> Dict[str, DepHealth]:
        """Check all critical deps. Override in tests."""
        results = {}
        for dep_name in CRITICAL_FOR_REASONING:
            if dep_name == "jprime_lifecycle":
                results[dep_name] = await self._check_jprime()
            elif dep_name == "proactive_detector":
                results[dep_name] = self._check_detector()
            else:
                results[dep_name] = await self._check_agent(dep_name)
        return results

    async def _check_jprime(self) -> DepHealth:
        try:
            from backend.core.jprime_lifecycle_controller import get_jprime_lifecycle_controller, LifecycleState
            ctrl = get_jprime_lifecycle_controller()
            s = ctrl.state
            if s == LifecycleState.READY:
                return DepHealth("jprime_lifecycle", DepStatus.HEALTHY)
            elif s == LifecycleState.DEGRADED:
                return DepHealth("jprime_lifecycle", DepStatus.DEGRADED)
            else:
                return DepHealth("jprime_lifecycle", DepStatus.UNAVAILABLE, error=s.value)
        except Exception as exc:
            return DepHealth("jprime_lifecycle", DepStatus.UNAVAILABLE, error=str(exc))

    def _check_detector(self) -> DepHealth:
        try:
            from backend.core.proactive_command_detector import get_proactive_detector
            det = get_proactive_detector()
            return DepHealth("proactive_detector", DepStatus.HEALTHY)
        except Exception as exc:
            return DepHealth("proactive_detector", DepStatus.UNAVAILABLE, error=str(exc))

    async def _check_agent(self, agent_name: str) -> DepHealth:
        try:
            from backend.neural_mesh.agents.agent_initializer import get_agent_initializer
            init = await get_agent_initializer()
            agent = init.get_agent(agent_name)
            if agent is None:
                return DepHealth(agent_name, DepStatus.UNAVAILABLE, error="not_initialized")
            start = time.monotonic()
            stats = await asyncio.wait_for(agent.execute_task({"action": "get_stats"}), timeout=2.0)
            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms > 1000:
                return DepHealth(agent_name, DepStatus.DEGRADED, response_time_ms=elapsed_ms)
            return DepHealth(agent_name, DepStatus.HEALTHY, response_time_ms=elapsed_ms)
        except Exception as exc:
            return DepHealth(agent_name, DepStatus.UNAVAILABLE, error=str(exc))

    async def _evaluate_deps(self) -> None:
        self._dep_statuses = await self._check_all_deps()
        all_healthy = all(d.status == DepStatus.HEALTHY for d in self._dep_statuses.values())
        any_unavailable = any(d.status == DepStatus.UNAVAILABLE for d in self._dep_statuses.values())
        any_degraded = any(d.status == DepStatus.DEGRADED for d in self._dep_statuses.values())

        if self._state == GateState.WAITING_DEPS:
            if all_healthy:
                await self._try_transition(GateState.READY, "deps_healthy", "ALL_DEPS_READY")
        elif self._state == GateState.ACTIVE:
            if any_unavailable:
                self._consecutive_failures += 1
                self._consecutive_healthy = 0
                if self._consecutive_failures >= self._config.block_threshold:
                    unavail = [n for n, d in self._dep_statuses.items() if d.status == DepStatus.UNAVAILABLE]
                    await self._try_transition(GateState.BLOCKED, "dep_unavailable", f"AGENT_UNAVAILABLE:{','.join(unavail)}")
            elif any_degraded:
                self._consecutive_degraded += 1
                self._consecutive_healthy = 0
                if self._consecutive_degraded >= self._config.degrade_threshold:
                    degraded = [n for n, d in self._dep_statuses.items() if d.status == DepStatus.DEGRADED]
                    await self._try_transition(GateState.DEGRADED, "dep_degraded", f"DEP_DEGRADED:{','.join(degraded)}")
            else:
                self._consecutive_failures = 0
                self._consecutive_degraded = 0
                self._consecutive_healthy += 1
        elif self._state == GateState.DEGRADED:
            if any_unavailable:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._config.block_threshold:
                    await self._try_transition(GateState.BLOCKED, "dep_unavailable", "MULTI_DEP_FAILURE")
            elif all_healthy:
                self._consecutive_healthy += 1
                self._consecutive_failures = 0
                if self._consecutive_healthy >= self._config.recovery_threshold:
                    await self._try_transition(GateState.ACTIVE, "deps_recovered", "ALL_DEPS_HEALTHY")
            else:
                self._consecutive_healthy = 0

    async def _evaluate_dwell(self) -> None:
        if self._state == GateState.READY:
            elapsed = time.monotonic() - self._state_entered_at
            if elapsed >= self._config.activation_dwell_s:
                # Re-check deps are still healthy
                deps = await self._check_all_deps()
                if all(d.status == DepStatus.HEALTHY for d in deps.values()):
                    await self._try_transition(GateState.ACTIVE, "dwell_complete", "ACTIVATION_ARMED")
                else:
                    await self._try_transition(GateState.WAITING_DEPS, "dep_lost_during_dwell", "DEP_LOST_DURING_ARM")

    async def _evaluate_block_duration(self) -> None:
        if self._state == GateState.BLOCKED:
            elapsed = time.monotonic() - self._state_entered_at
            if elapsed >= self._config.max_block_duration_s:
                await self._try_transition(GateState.TERMINAL, "sustained_block", "SUSTAINED_BLOCK")
            else:
                # Try recovery
                deps = await self._check_all_deps()
                self._dep_statuses = deps
                if all(d.status != DepStatus.UNAVAILABLE for d in deps.values()):
                    await self._try_transition(GateState.WAITING_DEPS, "block_recovery", "DEPS_RECOVERING")

    async def _evaluate_terminal_cooldown(self) -> None:
        if self._state == GateState.TERMINAL:
            elapsed = time.monotonic() - self._state_entered_at
            if elapsed >= self._config.terminal_cooldown_s:
                self._consecutive_failures = 0
                self._consecutive_degraded = 0
                self._consecutive_healthy = 0
                await self._try_transition(GateState.WAITING_DEPS, "cooldown_expired", "AUTO_RESET")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="reasoning_gate_poll")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._config.dep_poll_interval_s)
                await self._evaluate_flags()
                if self._state == GateState.DISABLED:
                    continue
                await self._evaluate_deps()
                await self._evaluate_dwell()
                await self._evaluate_block_duration()
                await self._evaluate_terminal_cooldown()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[Gate] Poll loop error: %s", exc)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None


_gate_instance: Optional[ReasoningActivationGate] = None


def get_reasoning_activation_gate() -> ReasoningActivationGate:
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = ReasoningActivationGate()
    return _gate_instance
```

- [ ] **Step 4: Run ALL tests**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_activation_gate.py tests/core/test_reasoning_activation_gate.py
git commit -m "feat(gate): implement ReasoningActivationGate 7-state FSM with dep polling"
```

---

### Task 3: Integrate Gate into ReasoningChainOrchestrator

**Files:**
- Modify: `backend/core/reasoning_chain_orchestrator.py:467-486`
- Test: `tests/core/test_reasoning_activation_gate.py` (append)

- [ ] **Step 1: Write failing test**

APPEND to `tests/core/test_reasoning_activation_gate.py`:

```python
class TestGateOrchestratorIntegration:
    @pytest.mark.asyncio
    async def test_orchestrator_checks_gate(self):
        """process() returns None when gate is not active."""
        from backend.core.reasoning_chain_orchestrator import (
            ReasoningChainOrchestrator, ChainConfig, ChainPhase,
        )
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        orch._detector = AsyncMock()
        orch._detector.detect.return_value = MagicMock(
            is_proactive=True, confidence=0.95,
            signals_detected=[], reasoning="test",
        )
        orch._planner = AsyncMock()

        # Mock gate as NOT active
        mock_gate = MagicMock()
        mock_gate.is_active.return_value = False
        with patch("backend.core.reasoning_chain_orchestrator.get_reasoning_activation_gate", return_value=mock_gate):
            result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None  # Gate blocked it

    @pytest.mark.asyncio
    async def test_orchestrator_passes_when_gate_active(self):
        """process() proceeds normally when gate is active."""
        from backend.core.reasoning_chain_orchestrator import (
            ReasoningChainOrchestrator, ChainConfig, ChainPhase,
        )
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        orch._detector = AsyncMock()
        orch._detector.detect.return_value = MagicMock(
            is_proactive=False, confidence=0.1,
            signals_detected=[], reasoning="test",
        )

        mock_gate = MagicMock()
        mock_gate.is_active.return_value = True
        mock_gate.state = GateState.ACTIVE
        mock_gate.get_degraded_config.return_value = {}
        with patch("backend.core.reasoning_chain_orchestrator.get_reasoning_activation_gate", return_value=mock_gate):
            result = await orch.process("what time is it", context={}, trace_id="t1")
        # Non-proactive command returns None (normal behavior, not gate-blocked)
        assert result is None
        orch._detector.detect.assert_called_once()  # Detector was called (gate didn't block)
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py::TestGateOrchestratorIntegration -v`
Expected: FAIL (gate check not in orchestrator yet)

- [ ] **Step 3: Add gate check to orchestrator process()**

Read `backend/core/reasoning_chain_orchestrator.py`. Find `process()` at line 467. After `start_ms = time.monotonic() * 1000` (line 480), insert:

```python
        # v300.2: Reasoning activation gate check
        try:
            from backend.core.reasoning_activation_gate import get_reasoning_activation_gate
            _gate = get_reasoning_activation_gate()
            if not _gate.is_active():
                return None  # Gate not active — fall through to single-intent

            # Apply degraded overrides if needed
            from backend.core.reasoning_activation_gate import GateState
            if _gate.state == GateState.DEGRADED:
                _overrides = _gate.get_degraded_config()
                if _overrides:
                    self._config.proactive_threshold += _overrides.get("proactive_threshold_boost", 0)
                    if "auto_expand_threshold" in _overrides:
                        self._config.auto_expand_threshold = _overrides["auto_expand_threshold"]
                    if "expansion_timeout_factor" in _overrides:
                        self._config.expansion_timeout *= _overrides["expansion_timeout_factor"]
        except Exception:
            pass  # Gate unavailable — proceed without gating
```

- [ ] **Step 4: Run ALL tests (gate + orchestrator + contract)**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py tests/core/test_reasoning_chain_orchestrator.py tests/core/test_telemetry_contract.py -v --tb=short`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_chain_orchestrator.py tests/core/test_reasoning_activation_gate.py
git commit -m "feat(gate): integrate activation gate into reasoning chain process()"
```

---

### Task 4: Wire Agent Init + Gate into Supervisor

**Files:**
- Modify: `unified_supervisor.py` (~line 74984, between Zone 6.5 and Zone 6.7)
- Test: `tests/core/test_reasoning_activation_gate.py` (append)

- [ ] **Step 1: Write integration test**

APPEND to `tests/core/test_reasoning_activation_gate.py`:

```python
class TestAgentInitIntegration:
    @pytest.mark.asyncio
    async def test_gate_starts_and_polls(self):
        """Gate starts polling and transitions from DISABLED."""
        config = GateConfig(
            activation_dwell_s=0.01,
            min_state_dwell_s=0.01,
            dep_poll_interval_s=0.05,
        )
        gate = ReasoningActivationGate(config=config)
        gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses())

        env = {"JARVIS_REASONING_CHAIN_ENABLED": "true"}
        with patch.dict("os.environ", env, clear=False):
            await gate.start()
            await asyncio.sleep(0.3)  # Let poll cycles run
            await gate.stop()

        # Should have progressed from DISABLED through WAITING_DEPS/READY to ACTIVE
        assert gate.state == GateState.ACTIVE
```

- [ ] **Step 2: Run test**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py::TestAgentInitIntegration -v`
Expected: PASS

- [ ] **Step 3: Wire into supervisor**

Find line ~74984 in `unified_supervisor.py` (end of Zone 6.5, Ghost Display). Before Zone 6.7 (AGI OS, line ~74987), insert:

```python
            # =================================================================
            # v300.2: Zone 6.55 — Neural Mesh Agent Initialization
            # Agents init independently of J-Prime. Non-fatal failures.
            # =================================================================
            self._current_startup_phase = "neural_mesh_agents"
            self._current_startup_progress = 85
            try:
                from backend.neural_mesh.agents.agent_initializer import (
                    initialize_production_agents,
                )
                from backend.neural_mesh.coordinator import NeuralMeshCoordinator

                _mesh_coordinator = NeuralMeshCoordinator()
                await _mesh_coordinator.initialize()
                await _mesh_coordinator.start()

                _agent_statuses = await initialize_production_agents(
                    _mesh_coordinator,
                )
                _initialized = len(_agent_statuses)
                self.logger.info(
                    "[Kernel] Zone 6.55: Neural Mesh agents initialized: %d/%d",
                    _initialized, _initialized,
                )

                # Emit scheduler.graph_state telemetry
                try:
                    from backend.core.telemetry_contract import TelemetryEnvelope, get_telemetry_bus
                    _graph_env = TelemetryEnvelope.create(
                        event_schema="scheduler.graph_state@1.0.0",
                        source="agent_initializer",
                        trace_id="boot",
                        span_id="agent_init",
                        partition_key="scheduler",
                        payload={
                            "total_agents": _initialized,
                            "initialized": _initialized,
                            "failed": 0,
                            "failed_agents": [],
                            "critical_ready": {
                                name: name in [a.lower().replace("agent", "").strip("_") for a in _agent_statuses]
                                for name in ["coordinator_agent", "predictive_planner", "proactive_detector"]
                            },
                        },
                    )
                    get_telemetry_bus().emit(_graph_env)
                except Exception:
                    pass

                # Connect to supervisor's neural mesh bridge
                self.connect_neural_mesh(_mesh_coordinator)

            except Exception as exc:
                self.logger.warning(
                    "[Kernel] Zone 6.55: Neural Mesh agent init failed (non-critical): %s", exc,
                )

            # =================================================================
            # v300.2: Zone 6.56 — Reasoning Activation Gate
            # Background polling — does not block boot.
            # =================================================================
            try:
                from backend.core.reasoning_activation_gate import (
                    get_reasoning_activation_gate,
                )
                _reasoning_gate = get_reasoning_activation_gate()
                await _reasoning_gate.start()
                self.logger.info("[Kernel] Zone 6.56: Reasoning activation gate started (state=%s)", _reasoning_gate.state.value)
            except Exception as exc:
                self.logger.warning(
                    "[Kernel] Zone 6.56: Reasoning gate failed (non-critical): %s", exc,
                )

```

- [ ] **Step 4: Run all gate tests**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py -v`
Expected: All PASS

- [ ] **Step 5: Run regression**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -q --tb=no --timeout=60 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add unified_supervisor.py tests/core/test_reasoning_activation_gate.py
git commit -m "feat(gate): wire agent init (Zone 6.55) and activation gate (Zone 6.56) into supervisor"
```

---

### Task 5: Final Regression and Verification

**Files:** All files (read-only verification)

- [ ] **Step 1: Run full gate test suite**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py -v --tb=short`
Expected: All PASS

- [ ] **Step 2: Run all related test suites**

Run: `python3 -m pytest tests/core/test_reasoning_activation_gate.py tests/core/test_reasoning_chain_orchestrator.py tests/core/test_jprime_lifecycle_controller.py tests/core/test_telemetry_contract.py -q --tb=no`
Expected: All PASS

- [ ] **Step 3: Run regression**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -q --tb=no --timeout=60 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 4: Verify gate defaults to DISABLED**

Run: `python3 -c "from backend.core.reasoning_activation_gate import get_reasoning_activation_gate; g = get_reasoning_activation_gate(); print(f'state={g.state.value}, active={g.is_active()}, seq={g.gate_sequence}')"`
Expected: `state=DISABLED, active=False, seq=0`

- [ ] **Step 5: Verify imports**

Run: `python3 -c "from backend.core.reasoning_activation_gate import ReasoningActivationGate; from backend.core.reasoning_chain_orchestrator import ReasoningChainOrchestrator; print('imports OK')"`
Expected: `imports OK`

- [ ] **Step 6: Final commit**

```bash
git add -A
git status
git commit -m "feat(gate): complete Neural Mesh boot + reasoning activation (Phase B)

Neural Mesh agents init at Zone 6.55 (15 agents, non-fatal).
ReasoningActivationGate (7-state FSM) starts at Zone 6.56.
Gate controls reasoning chain: ACTIVE/DEGRADED accept commands,
all other states fall through to single-intent path.

New: backend/core/reasoning_activation_gate.py
Modified: backend/core/reasoning_chain_orchestrator.py (gate check)
Modified: unified_supervisor.py (Zones 6.55 + 6.56)"
```
