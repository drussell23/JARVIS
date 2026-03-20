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


DEGRADED_OVERRIDES: Dict[str, float] = {
    "proactive_threshold_boost": 0.1,
    "auto_expand_threshold": 1.0,
    "expansion_timeout_factor": 0.5,
    "mind_request_timeout_factor": 0.5,
}


# ---------------------------------------------------------------------------
# Transition log entry type (for introspection / debugging)
# ---------------------------------------------------------------------------

_TransitionEntry = Dict[str, Any]

# Maximum number of transition log entries retained.
_MAX_TRANSITIONS_LOG = 200


# ---------------------------------------------------------------------------
# ReasoningActivationGate — 7-state FSM
# ---------------------------------------------------------------------------


class ReasoningActivationGate:
    """Controls whether the reasoning chain accepts commands.

    Lifecycle::

        DISABLED ─(flags on)──> WAITING_DEPS ─(all healthy)──> READY
            ^                       ^                             │
            │                       │                    (dwell timer)
            │                       │                             v
        TERMINAL <──(max block)── BLOCKED <──(unavail)──── ACTIVE
            │                       ^                        │  ^
            │(cooldown)             │(unavail)               │  │
            v                       │                        v  │
         WAITING_DEPS          DEGRADED <──(degraded)── ACTIVE  │
                                    │                           │
                                    └──(recovery)───────────────┘

    The gate polls dependency health at ``dep_poll_interval_s`` and uses
    consecutive-counter thresholds to decide transitions (no rolling windows).

    Thread safety: all state mutations go through ``_try_transition`` which
    holds an ``asyncio.Lock``.
    """

    def __init__(self, config: Optional[GateConfig] = None) -> None:
        self._config = config or GateConfig.from_env()
        self._state = GateState.DISABLED
        # Set initial timestamp far enough in past so first transition is never
        # suppressed by flap guard.
        self._state_entered_at: float = time.monotonic() - (
            self._config.min_state_dwell_s + 1.0
        )
        self._gate_sequence: int = 0
        self._lock = asyncio.Lock()

        # Dependency health cache
        self._dep_statuses: Dict[str, DepHealth] = {}

        # Consecutive counters for hysteresis
        self._consecutive_degraded: int = 0
        self._consecutive_failures: int = 0
        self._consecutive_healthy: int = 0

        # Block tracking
        self._block_entered_at: float = 0.0

        # Transition log (bounded deque)
        self._transitions_log: Deque[_TransitionEntry] = deque(
            maxlen=_MAX_TRANSITIONS_LOG
        )

        # Background poll task
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

        # Trace ID for telemetry correlation
        self._trace_id: str = str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> GateState:
        """Current gate state (read-only)."""
        return self._state

    @property
    def gate_sequence(self) -> int:
        """Monotonically increasing counter bumped on every transition."""
        return self._gate_sequence

    def is_active(self) -> bool:
        """Return True if the gate currently accepts reasoning commands."""
        return self._state.accepts_commands

    def get_degraded_config(self) -> Dict[str, float]:
        """Return DEGRADED_OVERRIDES when degraded, empty dict otherwise."""
        if self._state == GateState.DEGRADED:
            return dict(DEGRADED_OVERRIDES)
        return {}

    # ------------------------------------------------------------------
    # Transition engine
    # ------------------------------------------------------------------

    async def _try_transition(
        self,
        to: GateState,
        trigger: str,
        cause_code: str,
    ) -> bool:
        """Attempt to transition to *to*.

        Returns False if:
        - Already in *to* (noop).
        - Flap suppression: less than ``min_state_dwell_s`` since last entry.

        On success: resets relevant counters, increments gate_sequence,
        emits telemetry, and logs.
        """
        async with self._lock:
            if self._state == to:
                return False

            # Flap suppression
            elapsed = time.monotonic() - self._state_entered_at
            if elapsed < self._config.min_state_dwell_s:
                logger.debug(
                    "[Gate] Flap suppressed: %s -> %s after %.3fs (min %.3fs)",
                    self._state.value,
                    to.value,
                    elapsed,
                    self._config.min_state_dwell_s,
                )
                return False

            from_state = self._state
            self._state = to
            self._state_entered_at = time.monotonic()
            self._gate_sequence += 1

            # Counter resets on state entry
            if to in (GateState.ACTIVE, GateState.WAITING_DEPS):
                self._consecutive_degraded = 0
                self._consecutive_failures = 0
                self._consecutive_healthy = 0

            if to == GateState.BLOCKED:
                self._block_entered_at = time.monotonic()

            # Log entry
            entry: _TransitionEntry = {
                "from": from_state.value,
                "to": to.value,
                "trigger": trigger,
                "cause_code": cause_code,
                "gate_sequence": self._gate_sequence,
                "timestamp": time.monotonic(),
            }
            self._transitions_log.append(entry)

            logger.info(
                "[Gate] %s -> %s (trigger=%s, cause=%s, seq=%d)",
                from_state.value,
                to.value,
                trigger,
                cause_code,
                self._gate_sequence,
            )

        # Telemetry (outside lock to avoid blocking)
        self._emit_transition(from_state, to, trigger, cause_code)
        return True

    # ------------------------------------------------------------------
    # Dependency evaluation
    # ------------------------------------------------------------------

    async def _check_all_deps(self) -> Dict[str, DepHealth]:
        """Probe each critical dependency and return health map.

        This method is designed to be INJECTABLE for tests: tests replace it
        with an AsyncMock returning a dict of DepHealth objects.
        """
        results: Dict[str, DepHealth] = {}
        for dep_name in CRITICAL_FOR_REASONING:
            if dep_name == "jprime_lifecycle":
                results[dep_name] = await self._check_jprime()
            elif dep_name == "proactive_detector":
                results[dep_name] = await self._check_detector()
            else:
                results[dep_name] = await self._check_agent(dep_name)
        return results

    async def _check_jprime(self) -> DepHealth:
        """Check J-Prime lifecycle controller state."""
        t0 = time.monotonic()
        try:
            from backend.core.jprime_lifecycle_controller import (
                get_jprime_lifecycle_controller,
            )

            ctrl = get_jprime_lifecycle_controller()
            state = ctrl.state
            elapsed_ms = (time.monotonic() - t0) * 1000.0

            # Map lifecycle state to dep status
            state_val = state.value if hasattr(state, "value") else str(state)
            if state_val in ("READY",):
                status = DepStatus.HEALTHY
            elif state_val in ("DEGRADED", "RECOVERING", "PROBING"):
                status = DepStatus.DEGRADED
            else:
                status = DepStatus.UNAVAILABLE

            return DepHealth(
                name="jprime_lifecycle",
                status=status,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return DepHealth(
                name="jprime_lifecycle",
                status=DepStatus.UNAVAILABLE,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
                error=str(exc),
            )

    async def _check_detector(self) -> DepHealth:
        """Check ProactiveCommandDetector singleton exists."""
        t0 = time.monotonic()
        try:
            from backend.core.proactive_command_detector import (
                get_proactive_detector,
            )

            detector = get_proactive_detector()
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            if detector is not None:
                return DepHealth(
                    name="proactive_detector",
                    status=DepStatus.HEALTHY,
                    last_check=time.monotonic(),
                    response_time_ms=elapsed_ms,
                )
            return DepHealth(
                name="proactive_detector",
                status=DepStatus.UNAVAILABLE,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
                error="detector is None",
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return DepHealth(
                name="proactive_detector",
                status=DepStatus.UNAVAILABLE,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
                error=str(exc),
            )

    async def _check_agent(self, name: str) -> DepHealth:
        """Check an agent by calling execute_task with get_stats action."""
        t0 = time.monotonic()
        try:
            from backend.neural_mesh.agents.agent_initializer import (
                get_agent_initializer,
            )

            initializer = await get_agent_initializer()
            agents = initializer.get_agents()  # type: ignore[union-attr]
            agent = agents.get(name)
            if agent is None:
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                return DepHealth(
                    name=name,
                    status=DepStatus.UNAVAILABLE,
                    last_check=time.monotonic(),
                    response_time_ms=elapsed_ms,
                    error=f"agent '{name}' not found",
                )

            # Probe with timeout
            await asyncio.wait_for(
                agent.execute_task({"action": "get_stats"}),
                timeout=2.0,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return DepHealth(
                name=name,
                status=DepStatus.HEALTHY,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
            )
        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return DepHealth(
                name=name,
                status=DepStatus.DEGRADED,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
                error="probe timed out (2s)",
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return DepHealth(
                name=name,
                status=DepStatus.UNAVAILABLE,
                last_check=time.monotonic(),
                response_time_ms=elapsed_ms,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # State evaluators (called by poll loop)
    # ------------------------------------------------------------------

    async def _evaluate_flags(self) -> None:
        """Check feature flags.  DISABLED <-> WAITING_DEPS."""
        enabled = _env_bool("JARVIS_REASONING_CHAIN_ENABLED")
        shadow = _env_bool("JARVIS_REASONING_CHAIN_SHADOW")
        flags_on = enabled or shadow

        if self._state == GateState.DISABLED and flags_on:
            await self._try_transition(
                GateState.WAITING_DEPS, "flags", "feature_flags_enabled"
            )
        elif self._state == GateState.WAITING_DEPS and not flags_on:
            await self._try_transition(
                GateState.DISABLED, "flags", "feature_flags_disabled"
            )

    async def _evaluate_deps(self) -> None:
        """Check dependency health, manage counters, trigger transitions."""
        self._dep_statuses = await self._check_all_deps()

        all_healthy = all(
            d.status == DepStatus.HEALTHY for d in self._dep_statuses.values()
        )
        any_degraded = any(
            d.status == DepStatus.DEGRADED for d in self._dep_statuses.values()
        )
        any_unavailable = any(
            d.status == DepStatus.UNAVAILABLE for d in self._dep_statuses.values()
        )

        # Update consecutive counters
        if all_healthy:
            self._consecutive_healthy += 1
            self._consecutive_degraded = 0
            self._consecutive_failures = 0
        elif any_unavailable:
            self._consecutive_failures += 1
            self._consecutive_healthy = 0
            # Don't reset degraded — unavailable is worse
        elif any_degraded:
            self._consecutive_degraded += 1
            self._consecutive_healthy = 0
            self._consecutive_failures = 0

        # State-specific transitions based on counters
        if self._state == GateState.WAITING_DEPS:
            if all_healthy:
                await self._try_transition(
                    GateState.READY, "deps", "all_deps_healthy"
                )

        elif self._state == GateState.ACTIVE:
            if self._consecutive_failures >= self._config.block_threshold:
                await self._try_transition(
                    GateState.BLOCKED, "deps", "dep_unavailable"
                )
            elif self._consecutive_degraded >= self._config.degrade_threshold:
                await self._try_transition(
                    GateState.DEGRADED, "deps", "dep_degraded"
                )

        elif self._state == GateState.DEGRADED:
            if self._consecutive_failures >= self._config.block_threshold:
                await self._try_transition(
                    GateState.BLOCKED, "deps", "dep_unavailable_from_degraded"
                )
            elif self._consecutive_healthy >= self._config.recovery_threshold:
                await self._try_transition(
                    GateState.ACTIVE, "deps", "deps_recovered"
                )

        elif self._state == GateState.BLOCKED:
            # Recovery from BLOCKED goes back to WAITING_DEPS (re-verify)
            if self._consecutive_healthy >= self._config.recovery_threshold:
                await self._try_transition(
                    GateState.WAITING_DEPS, "deps", "deps_recovered_from_block"
                )

    async def _evaluate_dwell(self) -> None:
        """READY -> ACTIVE after activation_dwell_s (re-checks deps)."""
        if self._state != GateState.READY:
            return

        elapsed = time.monotonic() - self._state_entered_at
        if elapsed < self._config.activation_dwell_s:
            return

        # Re-verify deps before activating
        dep_snapshot = await self._check_all_deps()
        all_healthy = all(
            d.status == DepStatus.HEALTHY for d in dep_snapshot.values()
        )
        if all_healthy:
            await self._try_transition(
                GateState.ACTIVE, "dwell", "activation_dwell_complete"
            )
        else:
            # Deps went bad during dwell — back to WAITING_DEPS
            await self._try_transition(
                GateState.WAITING_DEPS, "dwell", "deps_failed_during_dwell"
            )

    async def _evaluate_block_duration(self) -> None:
        """BLOCKED -> TERMINAL after max_block_duration_s."""
        if self._state != GateState.BLOCKED:
            return

        elapsed = time.monotonic() - self._block_entered_at
        if elapsed >= self._config.max_block_duration_s:
            await self._try_transition(
                GateState.TERMINAL, "block_timer", "max_block_exceeded"
            )

    async def _evaluate_terminal_cooldown(self) -> None:
        """TERMINAL -> WAITING_DEPS after terminal_cooldown_s."""
        if self._state != GateState.TERMINAL:
            return

        elapsed = time.monotonic() - self._state_entered_at
        if elapsed >= self._config.terminal_cooldown_s:
            await self._try_transition(
                GateState.WAITING_DEPS, "terminal_cooldown", "cooldown_complete"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background dependency poll loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        logger.info("[Gate] Background poll loop started (interval=%.1fs)", self._config.dep_poll_interval_s)

    async def _poll_loop(self) -> None:
        """Background loop that evaluates all state conditions."""
        while self._running:
            try:
                await self._evaluate_flags()

                if self._state not in (GateState.DISABLED,):
                    await self._evaluate_deps()
                    await self._evaluate_dwell()
                    await self._evaluate_block_duration()
                    await self._evaluate_terminal_cooldown()

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Gate] Poll loop error")

            await asyncio.sleep(self._config.dep_poll_interval_s)

    async def stop(self) -> None:
        """Stop the background poll loop."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("[Gate] Background poll loop stopped")

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _emit_transition(
        self,
        from_state: GateState,
        to_state: GateState,
        trigger: str,
        cause_code: str,
    ) -> None:
        """Create and emit a TelemetryEnvelope for state transitions."""
        try:
            from backend.core.telemetry_contract import (
                TelemetryEnvelope,
                get_telemetry_bus,
            )

            envelope = TelemetryEnvelope.create(
                event_schema="reasoning.activation@1.0.0",
                source="ReasoningActivationGate",
                trace_id=self._trace_id,
                span_id=str(uuid.uuid4()),
                partition_key="reasoning_gate",
                payload={
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                    "trigger": trigger,
                    "cause_code": cause_code,
                    "gate_sequence": self._gate_sequence,
                },
                severity="info",
            )
            bus = get_telemetry_bus()
            bus.emit(envelope)
        except Exception:
            # Telemetry is best-effort — never crash the gate
            logger.debug("[Gate] Telemetry emission failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_gate_instance: Optional[ReasoningActivationGate] = None


def get_reasoning_activation_gate() -> ReasoningActivationGate:
    """Return the module-level ``ReasoningActivationGate`` singleton (lazy-created)."""
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = ReasoningActivationGate()
    return _gate_instance
