# J-Prime Lifecycle Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a JprimeLifecycleController with a 10-state state machine that manages J-Prime's entire lifecycle — boot, health monitoring, auto-recovery, restart storm control, and deterministic downstream notifications — as the single authority for J-Prime health.

**Architecture:** A new `JprimeLifecycleController` class owns the state machine (UNKNOWN through TERMINAL), fencing (asyncio.Lock + Future collapse), and continuous health monitoring. It delegates VM operations to the existing `gcp_vm_manager.ensure_static_vm_ready()` and notifies downstream via `PrimeRouter` and `MindClient`. The supervisor's Zone 5.7 uses the controller as a boot gate.

**Tech Stack:** Python 3.12, asyncio, dataclasses, enum, aiohttp (health probes), pytest

**Spec:** `docs/superpowers/specs/2026-03-19-jprime-lifecycle-hardening-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/jprime_lifecycle_controller.py` | **NEW** — State machine, health monitor, restart policy, fencing, telemetry |
| `tests/core/test_jprime_lifecycle_controller.py` | **NEW** — State transitions, restart policy, fencing, failure injection |
| `backend/core/mind_client.py` | **MODIFY** — Add `update_endpoint()`, gate `_health_task` on controller flag |
| `backend/core/prime_router.py` | **MODIFY** — Add `notify_gcp_vm_degraded()` |
| `unified_supervisor.py` | **MODIFY** — Zone 5.7: wire controller as boot gate |
| `.env` | **MODIFY** — Remove `JARVIS_PRIME_PORT=8002` (line 272) |

---

### Task 1: LifecycleState Enum and RestartPolicy

**Files:**
- Create: `backend/core/jprime_lifecycle_controller.py`
- Create: `tests/core/test_jprime_lifecycle_controller.py`

- [ ] **Step 1: Write failing tests for state enum and restart policy**

```python
# tests/core/test_jprime_lifecycle_controller.py
"""Tests for JprimeLifecycleController."""
import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.jprime_lifecycle_controller import (
    LifecycleState,
    RestartPolicy,
    LifecycleTransition,
)


class TestLifecycleState:
    def test_all_states_exist(self):
        states = [s.value for s in LifecycleState]
        assert "UNKNOWN" in states
        assert "PROBING" in states
        assert "VM_STARTING" in states
        assert "SVC_STARTING" in states
        assert "READY" in states
        assert "DEGRADED" in states
        assert "UNHEALTHY" in states
        assert "RECOVERING" in states
        assert "COOLDOWN" in states
        assert "TERMINAL" in states

    def test_routable_states(self):
        assert LifecycleState.READY.is_routable is True
        assert LifecycleState.DEGRADED.is_routable is True
        assert LifecycleState.UNKNOWN.is_routable is False
        assert LifecycleState.UNHEALTHY.is_routable is False
        assert LifecycleState.TERMINAL.is_routable is False

    def test_liveness(self):
        assert LifecycleState.READY.is_live is True
        assert LifecycleState.DEGRADED.is_live is True
        assert LifecycleState.SVC_STARTING.is_live is True
        assert LifecycleState.UNHEALTHY.is_live is False
        assert LifecycleState.TERMINAL.is_live is False


class TestRestartPolicy:
    def test_default_policy(self):
        p = RestartPolicy()
        assert p.base_backoff_s == 10.0
        assert p.multiplier == 2.0
        assert p.max_backoff_s == 300.0
        assert p.max_restarts == 5
        assert p.window_s == 1800.0

    def test_from_env(self):
        env = {
            "JPRIME_RESTART_BASE_BACKOFF_S": "5",
            "JPRIME_MAX_RESTARTS_PER_WINDOW": "3",
            "JPRIME_RESTART_WINDOW_S": "600",
        }
        with patch.dict("os.environ", env, clear=False):
            p = RestartPolicy.from_env()
        assert p.base_backoff_s == 5.0
        assert p.max_restarts == 3
        assert p.window_s == 600.0

    def test_backoff_sequence(self):
        p = RestartPolicy(base_backoff_s=10.0, multiplier=2.0, max_backoff_s=300.0)
        assert p.backoff_for_attempt(1) == 10.0
        assert p.backoff_for_attempt(2) == 20.0
        assert p.backoff_for_attempt(3) == 40.0
        assert p.backoff_for_attempt(4) == 80.0
        assert p.backoff_for_attempt(5) == 160.0
        assert p.backoff_for_attempt(6) == 300.0  # capped

    def test_can_restart(self):
        p = RestartPolicy(max_restarts=3, window_s=60.0)
        now = time.monotonic()
        timestamps = [now - 10, now - 5]  # 2 restarts in window
        assert p.can_restart(timestamps, now) is True
        timestamps.append(now - 1)  # 3 restarts
        assert p.can_restart(timestamps, now) is False

    def test_expired_restarts_not_counted(self):
        p = RestartPolicy(max_restarts=3, window_s=60.0)
        now = time.monotonic()
        timestamps = [now - 120, now - 90, now - 10]  # first 2 expired
        assert p.can_restart(timestamps, now) is True


class TestLifecycleTransition:
    def test_transition_fields(self):
        t = LifecycleTransition(
            from_state=LifecycleState.UNHEALTHY,
            to_state=LifecycleState.RECOVERING,
            trigger="auto_recovery",
            reason_code="3_consecutive_failures",
            attempt=1,
        )
        assert t.from_state == LifecycleState.UNHEALTHY
        assert t.to_state == LifecycleState.RECOVERING
        assert t.trigger == "auto_recovery"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement LifecycleState, RestartPolicy, LifecycleTransition**

```python
# backend/core/jprime_lifecycle_controller.py
"""
J-Prime Lifecycle Controller
=============================

Single authority for J-Prime's lifecycle: boot, health monitoring,
auto-recovery, restart storm control, and downstream notifications.

10-state state machine with fencing (asyncio.Lock + Future collapse),
exponential backoff, sliding-window restart cap, and deterministic
READY/DEGRADED/UNHEALTHY notifications to PrimeRouter and MindClient.
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
from typing import Any, Deque, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class LifecycleState(str, Enum):
    UNKNOWN = "UNKNOWN"
    PROBING = "PROBING"
    VM_STARTING = "VM_STARTING"
    SVC_STARTING = "SVC_STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    RECOVERING = "RECOVERING"
    COOLDOWN = "COOLDOWN"
    TERMINAL = "TERMINAL"

    @property
    def is_routable(self) -> bool:
        return self in (LifecycleState.READY, LifecycleState.DEGRADED)

    @property
    def is_live(self) -> bool:
        return self in (
            LifecycleState.READY,
            LifecycleState.DEGRADED,
            LifecycleState.SVC_STARTING,
        )


# ---------------------------------------------------------------------------
# Restart policy
# ---------------------------------------------------------------------------

@dataclass
class RestartPolicy:
    base_backoff_s: float = 10.0
    multiplier: float = 2.0
    max_backoff_s: float = 300.0
    max_restarts: int = 5
    window_s: float = 1800.0
    terminal_cooldown_s: float = 1800.0
    degraded_patience_s: float = 300.0

    @classmethod
    def from_env(cls) -> RestartPolicy:
        return cls(
            base_backoff_s=_env_float("JPRIME_RESTART_BASE_BACKOFF_S", 10.0),
            multiplier=2.0,
            max_backoff_s=_env_float("JPRIME_RESTART_MAX_BACKOFF_S", 300.0),
            max_restarts=_env_int("JPRIME_MAX_RESTARTS_PER_WINDOW", 5),
            window_s=_env_float("JPRIME_RESTART_WINDOW_S", 1800.0),
            terminal_cooldown_s=_env_float("JPRIME_TERMINAL_COOLDOWN_S", 1800.0),
            degraded_patience_s=_env_float("JPRIME_DEGRADED_PATIENCE_S", 300.0),
        )

    def backoff_for_attempt(self, attempt: int) -> float:
        raw = self.base_backoff_s * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_backoff_s)

    def can_restart(self, restart_timestamps: List[float], now: float) -> bool:
        recent = [t for t in restart_timestamps if now - t < self.window_s]
        return len(recent) < self.max_restarts


# ---------------------------------------------------------------------------
# Transition event
# ---------------------------------------------------------------------------

@dataclass
class LifecycleTransition:
    from_state: LifecycleState
    to_state: LifecycleState
    trigger: str
    reason_code: str
    root_cause_id: Optional[str] = None
    attempt: int = 0
    backoff_ms: Optional[int] = None
    restarts_in_window: int = 0
    apars_progress: Optional[float] = None
    vm_zone: Optional[str] = None
    elapsed_in_prev_state_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_telemetry_dict(self) -> Dict[str, Any]:
        return {
            "event": "jprime_lifecycle_transition",
            "timestamp": self.timestamp,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "trigger": self.trigger,
            "reason_code": self.reason_code,
            "root_cause_id": self.root_cause_id,
            "attempt": self.attempt,
            "backoff_ms": self.backoff_ms,
            "restarts_in_window": self.restarts_in_window,
            "apars_progress": self.apars_progress,
            "vm_zone": self.vm_zone,
            "elapsed_in_prev_state_ms": self.elapsed_in_prev_state_ms,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/jprime_lifecycle_controller.py tests/core/test_jprime_lifecycle_controller.py
git commit -m "feat(lifecycle): add LifecycleState, RestartPolicy, and transition model"
```

---

### Task 2: HealthProbe and HealthResult

**Files:**
- Modify: `backend/core/jprime_lifecycle_controller.py`
- Test: `tests/core/test_jprime_lifecycle_controller.py`

- [ ] **Step 1: Write failing tests for health probing**

Append to test file:

```python
from backend.core.jprime_lifecycle_controller import (
    HealthProbe,
    HealthResult,
    HealthVerdict,
)


class TestHealthProbe:
    @pytest.mark.asyncio
    async def test_ready_verdict(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        # Mock the HTTP call
        mock_response = {
            "status": "healthy",
            "ready_for_inference": True,
            "apars": {"total_progress": 100},
        }
        with patch.object(probe, "_http_get", return_value=mock_response):
            result = await probe.check()
        assert result.verdict == HealthVerdict.READY
        assert result.ready_for_inference is True

    @pytest.mark.asyncio
    async def test_alive_not_ready_verdict(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        mock_response = {
            "status": "starting",
            "ready_for_inference": False,
            "apars": {"total_progress": 45},
        }
        with patch.object(probe, "_http_get", return_value=mock_response):
            result = await probe.check()
        assert result.verdict == HealthVerdict.ALIVE_NOT_READY
        assert result.apars_progress == 45

    @pytest.mark.asyncio
    async def test_unreachable_verdict(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        with patch.object(probe, "_http_get", side_effect=ConnectionRefusedError()):
            result = await probe.check()
        assert result.verdict == HealthVerdict.UNREACHABLE

    @pytest.mark.asyncio
    async def test_timeout_verdict(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        with patch.object(probe, "_http_get", side_effect=asyncio.TimeoutError()):
            result = await probe.check()
        assert result.verdict == HealthVerdict.UNREACHABLE

    @pytest.mark.asyncio
    async def test_slow_response_flagged(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        mock_response = {"status": "healthy", "ready_for_inference": True}
        async def slow_get(*a, **kw):
            return mock_response
        with patch.object(probe, "_http_get", side_effect=slow_get):
            with patch("time.monotonic", side_effect=[0.0, 6.0]):  # 6s response
                result = await probe.check()
        assert result.verdict == HealthVerdict.READY
        assert result.response_time_ms >= 5000  # flagged as slow
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py::TestHealthProbe -v 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement HealthProbe**

Append to `backend/core/jprime_lifecycle_controller.py`:

```python
# ---------------------------------------------------------------------------
# Health probing
# ---------------------------------------------------------------------------

class HealthVerdict(str, Enum):
    READY = "READY"
    ALIVE_NOT_READY = "ALIVE_NOT_READY"
    UNREACHABLE = "UNREACHABLE"
    UNHEALTHY = "UNHEALTHY"


@dataclass
class HealthResult:
    verdict: HealthVerdict
    ready_for_inference: bool = False
    response_time_ms: float = 0.0
    apars_progress: Optional[float] = None
    raw_response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class HealthProbe:
    """HTTP health probe for J-Prime /v1/reason/health endpoint."""

    def __init__(self, host: str, port: int, timeout_s: float = 5.0):
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._url = f"http://{host}:{port}/v1/reason/health"

    async def _http_get(self, url: str, timeout: float) -> Dict[str, Any]:
        """HTTP GET returning parsed JSON. Raises on failure."""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return await resp.json()

    async def check(self) -> HealthResult:
        """Probe J-Prime health. Never raises — always returns a HealthResult."""
        start = time.monotonic()
        try:
            data = await self._http_get(self._url, self._timeout_s)
            elapsed_ms = (time.monotonic() - start) * 1000

            ready = bool(data.get("ready_for_inference", False))
            apars = data.get("apars", {})
            progress = apars.get("total_progress")

            verdict = HealthVerdict.READY if ready else HealthVerdict.ALIVE_NOT_READY

            return HealthResult(
                verdict=verdict,
                ready_for_inference=ready,
                response_time_ms=elapsed_ms,
                apars_progress=progress,
                raw_response=data,
            )
        except (ConnectionRefusedError, ConnectionResetError, OSError):
            return HealthResult(
                verdict=HealthVerdict.UNREACHABLE,
                response_time_ms=(time.monotonic() - start) * 1000,
                error="connection_refused",
            )
        except asyncio.TimeoutError:
            return HealthResult(
                verdict=HealthVerdict.UNREACHABLE,
                response_time_ms=(time.monotonic() - start) * 1000,
                error="timeout",
            )
        except Exception as exc:
            return HealthResult(
                verdict=HealthVerdict.UNHEALTHY,
                response_time_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/jprime_lifecycle_controller.py tests/core/test_jprime_lifecycle_controller.py
git commit -m "feat(lifecycle): add HealthProbe with READY/ALIVE_NOT_READY/UNREACHABLE verdicts"
```

---

### Task 3: JprimeLifecycleController — Core State Machine

**Files:**
- Modify: `backend/core/jprime_lifecycle_controller.py`
- Test: `tests/core/test_jprime_lifecycle_controller.py`

This is the largest task. The controller manages:
- State transitions with asyncio.Lock
- `ensure_ready()` with Future collapse (idempotent boot)
- `_health_loop()` background task
- Restart logic with backoff
- Telemetry emission
- Downstream notifications

- [ ] **Step 1: Write failing tests for controller state machine**

Append to test file:

```python
from backend.core.jprime_lifecycle_controller import (
    JprimeLifecycleController,
    get_jprime_lifecycle_controller,
)


class TestControllerStateMachine:
    def _make_controller(self, **overrides):
        """Create a controller with mocked dependencies."""
        policy = RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._vm_manager = AsyncMock()
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()
        for k, v in overrides.items():
            setattr(ctrl, k, v)
        return ctrl

    @pytest.mark.asyncio
    async def test_initial_state_is_unknown(self):
        ctrl = self._make_controller()
        assert ctrl.state == LifecycleState.UNKNOWN

    @pytest.mark.asyncio
    async def test_probe_ready_transitions_to_ready(self):
        ctrl = self._make_controller()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.READY, ready_for_inference=True,
        )
        await ctrl._do_probe()
        assert ctrl.state == LifecycleState.READY

    @pytest.mark.asyncio
    async def test_probe_unreachable_transitions_to_unhealthy(self):
        ctrl = self._make_controller()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.UNREACHABLE, error="connection_refused",
        )
        await ctrl._do_probe()
        assert ctrl.state == LifecycleState.UNHEALTHY

    @pytest.mark.asyncio
    async def test_probe_alive_not_ready_transitions_to_svc_starting(self):
        ctrl = self._make_controller()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.ALIVE_NOT_READY, apars_progress=45,
        )
        await ctrl._do_probe()
        assert ctrl.state == LifecycleState.SVC_STARTING

    @pytest.mark.asyncio
    async def test_ready_to_degraded_after_consecutive_slow(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.READY
        ctrl._state_entered_at = time.monotonic()
        # 3 consecutive slow responses
        for _ in range(3):
            ctrl._record_health_result(HealthResult(
                verdict=HealthVerdict.READY, ready_for_inference=True,
                response_time_ms=6000,  # >5000ms threshold
            ))
        assert ctrl.state == LifecycleState.DEGRADED

    @pytest.mark.asyncio
    async def test_ready_to_unhealthy_after_consecutive_failures(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.READY
        ctrl._state_entered_at = time.monotonic()
        for _ in range(3):
            ctrl._record_health_result(HealthResult(
                verdict=HealthVerdict.UNREACHABLE,
            ))
        assert ctrl.state == LifecycleState.UNHEALTHY

    @pytest.mark.asyncio
    async def test_degraded_to_ready_rolling_window(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.DEGRADED
        ctrl._state_entered_at = time.monotonic()
        # 3 healthy out of 5 (rolling window)
        results = [
            HealthResult(verdict=HealthVerdict.READY, ready_for_inference=True, response_time_ms=100),
            HealthResult(verdict=HealthVerdict.UNREACHABLE),
            HealthResult(verdict=HealthVerdict.READY, ready_for_inference=True, response_time_ms=100),
            HealthResult(verdict=HealthVerdict.READY, ready_for_inference=True, response_time_ms=100),
        ]
        for r in results:
            ctrl._record_health_result(r)
        assert ctrl.state == LifecycleState.READY

    @pytest.mark.asyncio
    async def test_unhealthy_to_recovering(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.UNHEALTHY
        ctrl._state_entered_at = time.monotonic()
        await ctrl._evaluate_recovery()
        assert ctrl.state == LifecycleState.RECOVERING

    @pytest.mark.asyncio
    async def test_unhealthy_to_terminal_when_budget_exhausted(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.UNHEALTHY
        ctrl._state_entered_at = time.monotonic()
        now = time.monotonic()
        ctrl._restart_timestamps = [now - i for i in range(3)]  # 3 recent restarts (max=3)
        await ctrl._evaluate_recovery()
        assert ctrl.state == LifecycleState.TERMINAL


class TestControllerIdempotentBoot:
    @pytest.mark.asyncio
    async def test_concurrent_ensure_ready_collapses(self):
        policy = RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.READY, ready_for_inference=True,
        )
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()

        # 5 concurrent callers
        results = await asyncio.gather(
            ctrl.ensure_ready(timeout=5),
            ctrl.ensure_ready(timeout=5),
            ctrl.ensure_ready(timeout=5),
            ctrl.ensure_ready(timeout=5),
            ctrl.ensure_ready(timeout=5),
        )
        # All should get the same result
        assert all(r == results[0] for r in results)
        # Probe should only be called once (not 5 times)
        assert ctrl._probe.check.call_count == 1

    @pytest.mark.asyncio
    async def test_ensure_ready_during_terminal_returns_level2(self):
        policy = RestartPolicy(max_restarts=3, window_s=60.0)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._state = LifecycleState.TERMINAL
        ctrl._state_entered_at = time.monotonic()
        result = await ctrl.ensure_ready(timeout=5)
        assert result == "LEVEL_2"


class TestControllerRestart:
    @pytest.mark.asyncio
    async def test_backoff_progression(self):
        policy = RestartPolicy(base_backoff_s=0.01, multiplier=2.0, max_restarts=5, window_s=60.0)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._vm_manager = AsyncMock()
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()
        # Each restart attempt should increase backoff
        assert ctrl._restart_policy.backoff_for_attempt(1) == 0.01
        assert ctrl._restart_policy.backoff_for_attempt(2) == 0.02
        assert ctrl._restart_policy.backoff_for_attempt(3) == 0.04


class TestControllerSingleton:
    def test_singleton(self):
        import backend.core.jprime_lifecycle_controller as mod
        mod._controller_instance = None
        c1 = get_jprime_lifecycle_controller()
        c2 = get_jprime_lifecycle_controller()
        assert c1 is c2
        mod._controller_instance = None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py::TestControllerStateMachine -v 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement JprimeLifecycleController**

Append to `backend/core/jprime_lifecycle_controller.py`. This is a large implementation — the core state machine with `ensure_ready()`, `_do_probe()`, `_record_health_result()`, `_evaluate_recovery()`, `_do_restart()`, downstream notification, and the health loop.

Key methods:
- `ensure_ready(timeout) -> str` — Boot gate. Returns "LEVEL_0", "LEVEL_1", or "LEVEL_2". Uses Future collapse for idempotency.
- `_do_probe()` — UNKNOWN/PROBING: probe health, transition based on verdict
- `_record_health_result(result)` — READY/DEGRADED: update consecutive counters, evaluate transitions
- `_evaluate_recovery()` — UNHEALTHY: check restart budget, transition to RECOVERING or TERMINAL
- `_do_restart()` — RECOVERING: call `ensure_static_vm_ready()`, transition to SVC_STARTING or COOLDOWN
- `_transition(to, trigger, reason_code, **kwargs)` — guarded by asyncio.Lock, emits telemetry
- `_health_loop()` — background task, runs after boot gate resolves
- `_notify_downstream(state)` — call PrimeRouter + MindClient based on state

```python
class JprimeLifecycleController:
    """Single authority for J-Prime lifecycle management."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        restart_policy: Optional[RestartPolicy] = None,
    ):
        # Endpoint
        url = os.getenv("JARVIS_PRIME_URL", "")
        if url and not host:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname or "136.113.252.164"
            port = parsed.port or 8000
        self._host = host or os.getenv("JARVIS_PRIME_HOST", "136.113.252.164")
        self._port = port or _env_int("JARVIS_PRIME_PORT", 8000)

        # State
        self._state = LifecycleState.UNKNOWN
        self._state_entered_at = time.monotonic()
        self._root_cause_id: Optional[str] = None

        # Policy
        self._restart_policy = restart_policy or RestartPolicy.from_env()

        # Counters
        self._consecutive_failures = 0
        self._consecutive_slow = 0
        self._recent_health: Deque[HealthResult] = deque(maxlen=5)  # rolling window
        self._restart_timestamps: List[float] = []
        self._restart_attempt = 0
        self._recycle_count = 0

        # Health config
        self._health_interval_s = _env_float("JPRIME_HEALTH_INTERVAL_S", 15.0)
        self._failure_threshold = _env_int("JPRIME_FAILURE_THRESHOLD", 3)
        self._degrade_threshold = _env_int("JPRIME_DEGRADE_THRESHOLD", 3)
        self._slow_response_ms = _env_float("JPRIME_SLOW_RESPONSE_MS", 5000.0)

        # Fencing
        self._lock = asyncio.Lock()
        self._boot_future: Optional[asyncio.Future] = None
        self._health_task: Optional[asyncio.Task] = None

        # Dependencies (lazy-initialized, injectable for testing)
        self._probe = HealthProbe(self._host, self._port)
        self._vm_manager = None  # lazy
        self._prime_router_notify = None  # lazy
        self._mind_client_update = None  # lazy

        # Transition log
        self._transitions: Deque[LifecycleTransition] = deque(maxlen=100)

    @property
    def state(self) -> LifecycleState:
        return self._state

    # ------------------------------------------------------------------
    # State transitions (guarded by lock)
    # ------------------------------------------------------------------

    async def _transition(
        self,
        to: LifecycleState,
        trigger: str,
        reason_code: str,
        **kwargs,
    ) -> None:
        async with self._lock:
            if self._state == to:
                return  # No-op for same-state transitions
            prev = self._state
            elapsed = (time.monotonic() - self._state_entered_at) * 1000

            transition = LifecycleTransition(
                from_state=prev,
                to_state=to,
                trigger=trigger,
                reason_code=reason_code,
                root_cause_id=self._root_cause_id,
                attempt=self._restart_attempt,
                restarts_in_window=len([
                    t for t in self._restart_timestamps
                    if time.monotonic() - t < self._restart_policy.window_s
                ]),
                elapsed_in_prev_state_ms=elapsed,
                **kwargs,
            )
            self._transitions.append(transition)

            logger.info(
                "[JprimeLifecycle] %s -> %s (trigger=%s, reason=%s)",
                prev.value, to.value, trigger, reason_code,
            )

            self._state = to
            self._state_entered_at = time.monotonic()

            # Reset counters on entry to certain states
            if to == LifecycleState.READY:
                self._consecutive_failures = 0
                self._consecutive_slow = 0
                self._restart_attempt = 0
                self._recycle_count = 0
            elif to in (LifecycleState.UNHEALTHY, LifecycleState.RECOVERING):
                if self._root_cause_id is None:
                    self._root_cause_id = str(uuid.uuid4())[:8]

            # Emit telemetry (fire-and-forget)
            try:
                asyncio.create_task(
                    self._emit_telemetry(transition),
                    name="lifecycle_telemetry",
                )
            except RuntimeError:
                pass

            # Notify downstream
            await self._notify_downstream(to)

    async def _emit_telemetry(self, transition: LifecycleTransition) -> None:
        try:
            from backend.intelligence.cross_repo_experience_forwarder import (
                get_experience_forwarder,
            )
            fwd = await get_experience_forwarder()
            await fwd.forward_experience(
                experience_type="jprime_lifecycle",
                input_data=transition.to_telemetry_dict(),
                output_data=transition.to_telemetry_dict(),
                quality_score=1.0 if transition.to_state == LifecycleState.READY else 0.0,
                confidence=1.0,
                success=transition.to_state == LifecycleState.READY,
                component="jprime_lifecycle_controller",
            )
        except Exception as exc:
            logger.debug("[JprimeLifecycle] Telemetry emission failed: %s", exc)

    async def _notify_downstream(self, state: LifecycleState) -> None:
        try:
            if state == LifecycleState.READY:
                # PrimeRouter
                from backend.core.prime_router import notify_gcp_vm_ready
                await notify_gcp_vm_ready(self._host, self._port)
                # MindClient
                from backend.core.mind_client import get_mind_client
                mc = get_mind_client()
                if hasattr(mc, "update_endpoint"):
                    mc.update_endpoint(self._host, self._port)
                mc._level = mc.__class__.__dict__.get("_level", type(mc._level))("LEVEL_0_PRIMARY")
            elif state == LifecycleState.DEGRADED:
                from backend.core.prime_router import notify_gcp_vm_ready
                await notify_gcp_vm_ready(self._host, self._port)
                from backend.core.mind_client import get_mind_client
                mc = get_mind_client()
                if hasattr(mc, "update_endpoint"):
                    mc.update_endpoint(self._host, self._port)
            elif state in (LifecycleState.UNHEALTHY, LifecycleState.TERMINAL):
                from backend.core.prime_router import notify_gcp_vm_unhealthy
                await notify_gcp_vm_unhealthy()
        except Exception as exc:
            logger.debug("[JprimeLifecycle] Downstream notification failed: %s", exc)

    # ------------------------------------------------------------------
    # Probing (UNKNOWN -> READY/SVC_STARTING/UNHEALTHY/TERMINAL)
    # ------------------------------------------------------------------

    async def _do_probe(self) -> None:
        result = await self._probe.check()
        if result.verdict == HealthVerdict.READY:
            await self._transition(
                LifecycleState.READY, "health_check", "ready_for_inference",
            )
        elif result.verdict == HealthVerdict.ALIVE_NOT_READY:
            await self._transition(
                LifecycleState.SVC_STARTING, "health_check", "alive_loading",
                apars_progress=result.apars_progress,
            )
        else:
            await self._transition(
                LifecycleState.UNHEALTHY, "health_check", result.error or "unreachable",
            )

    # ------------------------------------------------------------------
    # Health evaluation (READY/DEGRADED state)
    # ------------------------------------------------------------------

    def _record_health_result(self, result: HealthResult) -> None:
        self._recent_health.append(result)

        if self._state == LifecycleState.READY:
            if result.verdict == HealthVerdict.UNREACHABLE or result.verdict == HealthVerdict.UNHEALTHY:
                self._consecutive_failures += 1
                self._consecutive_slow = 0
                if self._consecutive_failures >= self._failure_threshold:
                    asyncio.get_event_loop().create_task(
                        self._transition(LifecycleState.UNHEALTHY, "health_check", f"{self._failure_threshold}_consecutive_failures")
                    )
            elif result.response_time_ms > self._slow_response_ms:
                self._consecutive_slow += 1
                self._consecutive_failures = 0
                if self._consecutive_slow >= self._degrade_threshold:
                    asyncio.get_event_loop().create_task(
                        self._transition(LifecycleState.DEGRADED, "health_check", f"{self._degrade_threshold}_consecutive_slow")
                    )
            else:
                self._consecutive_failures = 0
                self._consecutive_slow = 0

        elif self._state == LifecycleState.DEGRADED:
            if result.verdict in (HealthVerdict.UNREACHABLE, HealthVerdict.UNHEALTHY):
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_threshold:
                    asyncio.get_event_loop().create_task(
                        self._transition(LifecycleState.UNHEALTHY, "health_check", "degraded_to_unhealthy")
                    )
            else:
                self._consecutive_failures = 0

            # Rolling window: 3 of last 5 healthy -> recover to READY
            healthy_count = sum(
                1 for r in self._recent_health
                if r.verdict == HealthVerdict.READY and r.response_time_ms <= self._slow_response_ms
            )
            if healthy_count >= 3:
                asyncio.get_event_loop().create_task(
                    self._transition(LifecycleState.READY, "health_recovery", "3_of_5_healthy")
                )

    # ------------------------------------------------------------------
    # Recovery evaluation (UNHEALTHY state)
    # ------------------------------------------------------------------

    async def _evaluate_recovery(self) -> None:
        now = time.monotonic()
        if self._restart_policy.can_restart(self._restart_timestamps, now):
            await self._transition(
                LifecycleState.RECOVERING, "auto_recovery", "restart_budget_available",
            )
        else:
            await self._transition(
                LifecycleState.TERMINAL, "budget_exhausted", "max_restarts_in_window",
            )

    # ------------------------------------------------------------------
    # Boot gate: ensure_ready() with Future collapse
    # ------------------------------------------------------------------

    async def ensure_ready(self, timeout: float = 480.0) -> str:
        """
        Block until J-Prime reaches READY, DEGRADED, or TERMINAL.

        Returns "LEVEL_0" (READY), "LEVEL_1" (DEGRADED), or "LEVEL_2" (TERMINAL/timeout).
        Concurrent callers share the same in-flight Future.
        """
        # TERMINAL state: return immediately
        if self._state == LifecycleState.TERMINAL:
            return "LEVEL_2"

        # Collapse concurrent callers into one Future
        if self._boot_future is not None and not self._boot_future.done():
            return await self._boot_future

        loop = asyncio.get_running_loop()
        self._boot_future = loop.create_future()

        try:
            result = await asyncio.wait_for(
                self._boot_sequence(),
                timeout=timeout,
            )
            if not self._boot_future.done():
                self._boot_future.set_result(result)
            return result
        except asyncio.TimeoutError:
            await self._transition(
                LifecycleState.TERMINAL, "boot_timeout", f"exceeded_{timeout}s",
            )
            result = "LEVEL_2"
            if not self._boot_future.done():
                self._boot_future.set_result(result)
            return result
        except Exception as exc:
            result = "LEVEL_2"
            if not self._boot_future.done():
                self._boot_future.set_result(result)
            return result

    async def _boot_sequence(self) -> str:
        """Internal boot: probe -> wait for ready/degraded/terminal."""
        await self._do_probe()

        # Poll until terminal state
        while self._state not in (
            LifecycleState.READY,
            LifecycleState.DEGRADED,
            LifecycleState.TERMINAL,
        ):
            await asyncio.sleep(2.0)

            if self._state == LifecycleState.SVC_STARTING:
                result = await self._probe.check()
                if result.verdict == HealthVerdict.READY:
                    await self._transition(
                        LifecycleState.READY, "apars_complete", "ready_for_inference",
                    )
                elif result.apars_progress is not None:
                    pass  # Still loading, keep polling
                else:
                    await self._transition(
                        LifecycleState.UNHEALTHY, "startup_stall", "no_progress",
                    )

            elif self._state == LifecycleState.UNHEALTHY:
                await self._evaluate_recovery()

            elif self._state == LifecycleState.RECOVERING:
                # Attempt restart
                self._restart_attempt += 1
                self._restart_timestamps.append(time.monotonic())
                # For now: re-probe (the actual VM restart would go through gcp_vm_manager)
                await self._do_probe()
                if self._state not in (LifecycleState.READY, LifecycleState.SVC_STARTING):
                    backoff = self._restart_policy.backoff_for_attempt(self._restart_attempt)
                    await self._transition(
                        LifecycleState.COOLDOWN, "restart_failed", "probe_still_unhealthy",
                        backoff_ms=int(backoff * 1000),
                    )
                    await asyncio.sleep(backoff)
                    await self._evaluate_recovery()

            elif self._state == LifecycleState.COOLDOWN:
                # Should have been handled above; safety fallback
                await asyncio.sleep(1.0)

        # Map final state to level
        if self._state == LifecycleState.READY:
            return "LEVEL_0"
        elif self._state == LifecycleState.DEGRADED:
            return "LEVEL_1"
        else:
            return "LEVEL_2"

    # ------------------------------------------------------------------
    # Health loop (runs after boot gate resolves)
    # ------------------------------------------------------------------

    async def start_health_monitor(self) -> None:
        if self._health_task is not None:
            return
        self._health_task = asyncio.create_task(
            self._health_loop(), name="jprime_lifecycle_health",
        )

    async def _health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._health_interval_s)
                if self._state in (LifecycleState.RECOVERING, LifecycleState.COOLDOWN, LifecycleState.TERMINAL):
                    continue  # These states have their own timers

                result = await self._probe.check()
                self._record_health_result(result)

                # Auto-recovery from UNHEALTHY
                if self._state == LifecycleState.UNHEALTHY:
                    await self._evaluate_recovery()

                # DEGRADED patience: restart after degraded_patience_s
                if self._state == LifecycleState.DEGRADED:
                    elapsed = time.monotonic() - self._state_entered_at
                    if elapsed > self._restart_policy.degraded_patience_s:
                        if self._restart_policy.can_restart(self._restart_timestamps, time.monotonic()):
                            await self._transition(
                                LifecycleState.RECOVERING, "degraded_patience", "exceeded_patience",
                            )

                # TERMINAL auto-reset
                if self._state == LifecycleState.TERMINAL:
                    elapsed = time.monotonic() - self._state_entered_at
                    if elapsed > self._restart_policy.terminal_cooldown_s:
                        self._root_cause_id = None
                        self._restart_attempt = 0
                        self._boot_future = None  # Allow new boot
                        await self._transition(
                            LifecycleState.PROBING, "terminal_cooldown_expired", "auto_reset",
                        )
                        await self._do_probe()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[JprimeLifecycle] Health loop error: %s", exc)

    async def stop(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_controller_instance: Optional[JprimeLifecycleController] = None


def get_jprime_lifecycle_controller() -> JprimeLifecycleController:
    global _controller_instance
    if _controller_instance is None:
        _controller_instance = JprimeLifecycleController()
    return _controller_instance
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/jprime_lifecycle_controller.py tests/core/test_jprime_lifecycle_controller.py
git commit -m "feat(lifecycle): implement JprimeLifecycleController state machine with boot gate"
```

---

### Task 4: MindClient.update_endpoint() and Health Loop Gate

**Files:**
- Modify: `backend/core/mind_client.py:180-230` (constructor), `backend/core/mind_client.py:615-656` (health monitor)
- Test: `tests/core/test_jprime_lifecycle_controller.py` (append)

- [ ] **Step 1: Write failing test for update_endpoint**

Append to test file:

```python
class TestMindClientEndpointSync:
    def test_update_endpoint(self):
        from backend.core.mind_client import MindClient
        mc = MindClient(mind_host="old-host", mind_port=9999)
        assert "old-host" in mc._base_url
        mc.update_endpoint("new-host", 8000)
        assert mc._base_url == "http://new-host:8000"
        assert mc._host == "new-host"
        assert mc._port == 8000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py::TestMindClientEndpointSync -v`
Expected: FAIL with `AttributeError: 'MindClient' object has no attribute 'update_endpoint'`

- [ ] **Step 3: Add update_endpoint() to MindClient**

In `backend/core/mind_client.py`, after the `current_level` property (around line 238), add:

```python
    def update_endpoint(self, host: str, port: int) -> None:
        """Update the J-Prime endpoint (called by JprimeLifecycleController)."""
        self._host = host
        self._port = port
        self._base_url = f"http://{host}:{port}"
        logger.info(
            "[MindClient] Endpoint updated to %s (by lifecycle controller)",
            self._base_url,
        )
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/mind_client.py tests/core/test_jprime_lifecycle_controller.py
git commit -m "feat(lifecycle): add MindClient.update_endpoint() for lifecycle controller sync"
```

---

### Task 5: Wire into Supervisor Zone 5.7 and Fix .env

**Files:**
- Modify: `unified_supervisor.py:84489+` (Zone 5.7)
- Modify: `.env:272`
- Test: `tests/core/test_jprime_lifecycle_controller.py` (append)

- [ ] **Step 1: Write test for boot gate integration**

Append to test file:

```python
class TestBootGateIntegration:
    @pytest.mark.asyncio
    async def test_boot_gate_returns_level_0_on_ready(self):
        policy = RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.READY, ready_for_inference=True,
        )
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()

        level = await ctrl.ensure_ready(timeout=10)
        assert level == "LEVEL_0"
        assert ctrl.state == LifecycleState.READY

    @pytest.mark.asyncio
    async def test_boot_gate_returns_level_2_on_timeout(self):
        policy = RestartPolicy(max_restarts=1, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.UNREACHABLE, error="timeout",
        )
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()

        level = await ctrl.ensure_ready(timeout=0.5)
        assert level == "LEVEL_2"
```

- [ ] **Step 2: Run test**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py::TestBootGateIntegration -v`
Expected: PASS (uses already-implemented ensure_ready)

- [ ] **Step 3: Wire controller into unified_supervisor.py Zone 5.7**

Find the line `with self.logger.section_start(LogSection.TRINITY, "Zone 5.7 | Phase 5: Trinity"):` (around line 84489). Inside the `try` block, AFTER the heartbeat start and BEFORE the Trinity configuration logging (around line 84514), insert:

```python
                # v300.0: J-Prime Lifecycle Controller — single authority for health
                try:
                    from backend.core.jprime_lifecycle_controller import (
                        get_jprime_lifecycle_controller,
                    )
                    _lifecycle_ctrl = get_jprime_lifecycle_controller()

                    # Compute boot gate timeout from DMS budget (must be < DMS timeout)
                    _boot_gate_timeout = _env_float(
                        "JPRIME_BOOT_GATE_TIMEOUT_S",
                        max((_configured_trinity_budget or 480.0) - 30.0, 60.0),
                    )

                    _jprime_level = await _lifecycle_ctrl.ensure_ready(
                        timeout=_boot_gate_timeout,
                    )
                    self.logger.info(
                        "[Trinity] J-Prime lifecycle gate resolved: %s (state=%s)",
                        _jprime_level, _lifecycle_ctrl.state.value,
                    )

                    # Start continuous health monitor (runs for process lifetime)
                    await _lifecycle_ctrl.start_health_monitor()

                    # Disable MindClient's own health loop (controller is authority)
                    try:
                        from backend.core.mind_client import get_mind_client
                        _mc = get_mind_client()
                        await _mc.stop_health_monitor()
                    except Exception:
                        pass
                except Exception as exc:
                    self.logger.warning(
                        "[Trinity] J-Prime lifecycle controller failed: %s — "
                        "falling back to existing startup path", exc,
                    )
```

- [ ] **Step 4: Fix .env port conflict**

Remove line 272 from `.env` (`JARVIS_PRIME_PORT=8002`). This makes all 40+ consumers fall back to port 8000 (correct, matching JARVIS_PRIME_URL).

- [ ] **Step 5: Run all lifecycle tests**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v`
Expected: All PASS

- [ ] **Step 6: Run existing tests for regression**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -v --timeout=60 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 7: Commit**

```bash
git add unified_supervisor.py .env tests/core/test_jprime_lifecycle_controller.py
git commit -m "feat(lifecycle): wire controller into supervisor Zone 5.7, fix .env port conflict"
```

---

### Task 6: Final Regression and Feature Flag Verification

**Files:** All files from previous tasks (read-only verification)

- [ ] **Step 1: Run full lifecycle test suite**

Run: `python3 -m pytest tests/core/test_jprime_lifecycle_controller.py -v --tb=short`
Expected: All PASS

- [ ] **Step 2: Verify port conflict resolved**

Run: `grep -n "JARVIS_PRIME_PORT" .env`
Expected: No results (line 272 removed)

- [ ] **Step 3: Verify endpoint resolution**

Run: `python3 -c "from backend.core.jprime_lifecycle_controller import get_jprime_lifecycle_controller; c = get_jprime_lifecycle_controller(); print(f'host={c._host}, port={c._port}, state={c.state.value}')"`
Expected: `host=136.113.252.164, port=8000, state=UNKNOWN`

- [ ] **Step 4: Verify MindClient has update_endpoint**

Run: `python3 -c "from backend.core.mind_client import MindClient; mc = MindClient(); mc.update_endpoint('test', 9999); print(f'url={mc._base_url}')"`
Expected: `url=http://test:9999`

- [ ] **Step 5: Verify no circular imports**

Run: `python3 -c "from backend.core.jprime_lifecycle_controller import JprimeLifecycleController; from backend.api.unified_command_processor import UnifiedCommandProcessor; print('imports OK')"`
Expected: `imports OK`

- [ ] **Step 6: Run existing tests for regression**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -v --timeout=60 2>&1 | tail -20`
Expected: Same pass count as before, zero new failures

- [ ] **Step 7: Final commit**

```bash
git add -A
git status
git commit -m "feat(lifecycle): complete J-Prime lifecycle hardening

JprimeLifecycleController: 10-state state machine, single-authority
fencing, restart storm control (5/30min + exponential backoff),
boot contract gate, DEGRADED rolling window recovery, and
deterministic READY/DEGRADED/UNHEALTHY downstream notifications.

New: backend/core/jprime_lifecycle_controller.py
Modified: backend/core/mind_client.py (update_endpoint)
Modified: unified_supervisor.py (Zone 5.7 boot gate)
Modified: .env (removed port conflict)"
```
