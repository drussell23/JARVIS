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

from backend.core.telemetry_contract import TelemetryEnvelope, get_telemetry_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env-var helpers (safe parsing with fallback)
# ---------------------------------------------------------------------------

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
# LifecycleState enum
# ---------------------------------------------------------------------------

class LifecycleState(str, Enum):
    """10-state lifecycle for J-Prime service health.

    Two derived properties:
      - is_routable: safe to forward inference traffic
      - is_live: VM/service process believed to be running
    """

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
        """True when it is safe to route inference requests to J-Prime."""
        return self in _ROUTABLE_STATES

    @property
    def is_live(self) -> bool:
        """True when the VM/service process is believed to be running."""
        return self in _LIVE_STATES


# Pre-computed frozensets for O(1) membership checks.
_ROUTABLE_STATES = frozenset({LifecycleState.READY, LifecycleState.DEGRADED})
_LIVE_STATES = frozenset({
    LifecycleState.READY,
    LifecycleState.DEGRADED,
    LifecycleState.SVC_STARTING,
})


# ---------------------------------------------------------------------------
# RestartPolicy
# ---------------------------------------------------------------------------

@dataclass
class RestartPolicy:
    """Exponential-backoff restart policy with sliding-window cap.

    Fields:
        base_backoff_s:       Initial backoff duration in seconds.
        multiplier:           Exponential multiplier per attempt.
        max_backoff_s:        Ceiling for computed backoff.
        max_restarts:         Maximum restarts allowed within ``window_s``.
        window_s:             Sliding window length in seconds.
        terminal_cooldown_s:  How long to stay in TERMINAL before allowing retry.
        degraded_patience_s:  Time to tolerate DEGRADED before escalating.
    """

    base_backoff_s: float = 10.0
    multiplier: float = 2.0
    max_backoff_s: float = 300.0
    max_restarts: int = 5
    window_s: float = 1800.0
    terminal_cooldown_s: float = 1800.0
    degraded_patience_s: float = 300.0

    @classmethod
    def from_env(cls) -> RestartPolicy:
        """Build a policy from environment variables, falling back to defaults."""
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
        """Compute backoff for a given attempt number (1-indexed), capped at max."""
        raw = self.base_backoff_s * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_backoff_s)

    def can_restart(self, restart_timestamps: List[float], now: float) -> bool:
        """Return True if a restart is permitted under the sliding-window cap."""
        recent = [t for t in restart_timestamps if now - t < self.window_s]
        return len(recent) < self.max_restarts


# ---------------------------------------------------------------------------
# LifecycleTransition
# ---------------------------------------------------------------------------

@dataclass
class LifecycleTransition:
    """Immutable record of a single state transition, used for telemetry and audit."""

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
        """Serialize to a flat dict suitable for structured logging / Langfuse."""
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
        """Probe J-Prime health. Never raises -- always returns a HealthResult."""
        start = time.monotonic()
        try:
            data = await self._http_get(self._url, self._timeout_s)
            elapsed_ms = (time.monotonic() - start) * 1000

            ready = bool(data.get("ready_for_inference", False))
            apars = data.get("apars", {})
            progress = apars.get("total_progress") if apars else None

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


# ---------------------------------------------------------------------------
# JprimeLifecycleController — core state machine
# ---------------------------------------------------------------------------

# Threshold for considering a health response "slow" (ms).
_SLOW_RESPONSE_THRESHOLD_MS = _env_float("JPRIME_SLOW_THRESHOLD_MS", 5000.0)

# Number of consecutive failures before READY/DEGRADED -> UNHEALTHY.
_CONSECUTIVE_FAILURE_LIMIT = _env_int("JPRIME_CONSECUTIVE_FAILURE_LIMIT", 3)

# Number of consecutive slow responses before READY -> DEGRADED.
_CONSECUTIVE_SLOW_LIMIT = _env_int("JPRIME_CONSECUTIVE_SLOW_LIMIT", 3)

# Rolling window size for DEGRADED -> READY recovery (need 3 of last 5 healthy).
_ROLLING_WINDOW_SIZE = 5
_ROLLING_HEALTHY_THRESHOLD = 3

# Health monitor polling interval (seconds).
_HEALTH_POLL_INTERVAL_S = _env_float("JPRIME_HEALTH_POLL_INTERVAL_S", 15.0)

# Boot poll interval (seconds) -- faster during boot sequence.
_BOOT_POLL_INTERVAL_S = _env_float("JPRIME_BOOT_POLL_INTERVAL_S", 2.0)


class JprimeLifecycleController:
    """Single authority for J-Prime lifecycle management.

    Manages a 10-state FSM with:
    - Boot gate via ``ensure_ready()`` with Future collapse
    - Continuous health monitoring via ``start_health_monitor()``
    - Automatic recovery with exponential backoff
    - Downstream notifications to PrimeRouter and MindClient
    - Telemetry emission for every state transition
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        restart_policy: Optional[RestartPolicy] = None,
    ) -> None:
        # Resolve endpoint from explicit args or JARVIS_PRIME_URL env var.
        if host is not None and port is not None:
            self._host = host
            self._port = port
        else:
            url = os.getenv("JARVIS_PRIME_URL", "")
            if url:
                # Parse http://host:port
                from urllib.parse import urlparse
                parsed = urlparse(url)
                self._host = parsed.hostname or "127.0.0.1"
                self._port = parsed.port or 8000
            else:
                self._host = os.getenv("JPRIME_HOST", "127.0.0.1")
                self._port = _env_int("JPRIME_PORT", 8000)

        self._policy = restart_policy or RestartPolicy.from_env()

        # State machine core
        self._state: LifecycleState = LifecycleState.UNKNOWN
        self._state_entered_at: float = time.monotonic()
        self._lock = asyncio.Lock()

        # Telemetry and history
        self._transitions: Deque[LifecycleTransition] = deque(maxlen=100)
        self._root_cause_id: Optional[str] = None

        # Health tracking
        self._consecutive_failures: int = 0
        self._consecutive_slow: int = 0
        self._recent_health: Deque[HealthResult] = deque(maxlen=_ROLLING_WINDOW_SIZE)

        # Restart tracking
        self._restart_timestamps: List[float] = []
        self._restart_attempt: int = 0

        # Health probe (injectable for tests)
        self._probe: HealthProbe = HealthProbe(self._host, self._port)

        # Downstream notification callables (injectable for tests).
        # When None, _notify_downstream uses real lazy imports.
        self._prime_router_notify: Optional[Any] = None
        self._mind_client_update: Optional[Any] = None

        # Boot gate: Future collapse for concurrent ensure_ready() callers
        self._boot_future: Optional[asyncio.Future] = None
        self._boot_lock = asyncio.Lock()

        # Background health monitor
        self._monitor_task: Optional[asyncio.Task] = None

    # -- Properties ----------------------------------------------------------

    @property
    def state(self) -> LifecycleState:
        """Current lifecycle state (read-only)."""
        return self._state

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    # -- State transitions ---------------------------------------------------

    async def _transition(
        self,
        to: LifecycleState,
        trigger: str,
        reason_code: str,
        **kwargs: Any,
    ) -> None:
        """Guarded state transition with telemetry and downstream notification.

        No-op if ``to`` equals the current state.
        """
        async with self._lock:
            if self._state == to:
                return

            now_mono = time.monotonic()
            elapsed_ms = (now_mono - self._state_entered_at) * 1000

            from_state = self._state

            transition = LifecycleTransition(
                from_state=from_state,
                to_state=to,
                trigger=trigger,
                reason_code=reason_code,
                root_cause_id=self._root_cause_id,
                attempt=self._restart_attempt,
                restarts_in_window=len([
                    t for t in self._restart_timestamps
                    if now_mono - t < self._policy.window_s
                ]),
                elapsed_in_prev_state_ms=elapsed_ms,
                **kwargs,
            )

            self._state = to
            self._state_entered_at = now_mono
            self._transitions.append(transition)

            # Reset counters on entering READY
            if to == LifecycleState.READY:
                self._consecutive_failures = 0
                self._consecutive_slow = 0
                self._restart_attempt = 0

            logger.info(
                "[JprimeLifecycle] %s -> %s  trigger=%s reason=%s (%.0fms in prev)",
                from_state.value, to.value, trigger, reason_code, elapsed_ms,
            )

        # Fire-and-forget telemetry + downstream (outside lock)
        self._emit_telemetry(transition)
        await self._notify_downstream(to)

    def _emit_telemetry(self, transition: LifecycleTransition) -> None:
        """Fire-and-forget telemetry via structured logging + TelemetryBus."""
        try:
            logger.debug(
                "[JprimeLifecycle] telemetry: %s",
                transition.to_telemetry_dict(),
            )
            # v300.1: Emit to unified TelemetryBus
            envelope = TelemetryEnvelope.create(
                event_schema="lifecycle.transition@1.0.0",
                source="jprime_lifecycle_controller",
                trace_id=transition.root_cause_id or "",
                span_id=str(uuid.uuid4())[:8],
                partition_key="lifecycle",
                severity="warning" if transition.to_state in (
                    LifecycleState.UNHEALTHY, LifecycleState.TERMINAL,
                ) else "info",
                payload=transition.to_telemetry_dict(),
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            logger.debug("[JprimeLifecycle] telemetry emission failed", exc_info=True)

    async def _notify_downstream(self, state: LifecycleState) -> None:
        """Notify PrimeRouter and MindClient of state changes.

        Uses injectable callables when set (tests), otherwise lazy-imports
        from the real modules. Never crashes -- all errors are swallowed.
        """
        try:
            if state.is_routable:
                # Notify: J-Prime is routable
                if self._prime_router_notify is not None:
                    await self._prime_router_notify(self._host, self._port)
                else:
                    try:
                        from backend.core.prime_router import notify_gcp_vm_ready
                        await notify_gcp_vm_ready(self._host, self._port)
                    except ImportError:
                        logger.debug("[JprimeLifecycle] prime_router not available")
                    except Exception:
                        logger.warning(
                            "[JprimeLifecycle] prime_router notification failed",
                            exc_info=True,
                        )

                if self._mind_client_update is not None:
                    await self._mind_client_update(self._host, self._port)
                else:
                    try:
                        from backend.core.prime_client import PrimeClient
                        client = PrimeClient.get_instance()
                        if client is not None:
                            await client.update_endpoint(self._host, self._port)
                    except (ImportError, AttributeError):
                        logger.debug("[JprimeLifecycle] prime_client not available")
                    except Exception:
                        logger.warning(
                            "[JprimeLifecycle] mind_client notification failed",
                            exc_info=True,
                        )
            else:
                # Notify: J-Prime is NOT routable
                if self._prime_router_notify is not None:
                    await self._prime_router_notify(None, None)
                else:
                    try:
                        from backend.core.prime_router import notify_gcp_vm_unhealthy
                        await notify_gcp_vm_unhealthy()
                    except ImportError:
                        logger.debug("[JprimeLifecycle] prime_router not available")
                    except Exception:
                        logger.warning(
                            "[JprimeLifecycle] prime_router notification failed",
                            exc_info=True,
                        )

                if self._mind_client_update is not None:
                    await self._mind_client_update(None, None)
                else:
                    try:
                        from backend.core.prime_client import PrimeClient
                        client = PrimeClient.get_instance()
                        if client is not None:
                            await client.demote_to_fallback()
                    except (ImportError, AttributeError):
                        logger.debug("[JprimeLifecycle] prime_client not available")
                    except Exception:
                        logger.warning(
                            "[JprimeLifecycle] mind_client demotion failed",
                            exc_info=True,
                        )
        except Exception:
            logger.warning(
                "[JprimeLifecycle] downstream notification failed unexpectedly",
                exc_info=True,
            )

    # -- Health evaluation ---------------------------------------------------

    async def _do_probe(self) -> HealthResult:
        """Execute a single health probe and transition state accordingly.

        Used during boot sequence and in the continuous health loop.
        Maps HealthVerdict -> LifecycleState for initial probing.
        """
        result = await self._probe.check()

        if result.verdict == HealthVerdict.READY:
            await self._transition(
                LifecycleState.READY, "probe", "health_ready",
            )
        elif result.verdict == HealthVerdict.ALIVE_NOT_READY:
            await self._transition(
                LifecycleState.SVC_STARTING, "probe", "alive_not_ready",
                apars_progress=result.apars_progress,
            )
        elif result.verdict in (HealthVerdict.UNREACHABLE, HealthVerdict.UNHEALTHY):
            await self._transition(
                LifecycleState.UNHEALTHY, "probe", f"verdict_{result.verdict.value.lower()}",
            )

        return result

    async def _record_health_result(self, result: HealthResult) -> None:
        """Record a health result and evaluate state transitions.

        Called from the continuous health monitor when in READY or DEGRADED.
        Tracks consecutive failures, consecutive slow responses, and a
        rolling window for DEGRADED -> READY recovery.
        """
        self._recent_health.append(result)

        is_healthy = (
            result.verdict == HealthVerdict.READY
            and result.ready_for_inference
            and result.response_time_ms < _SLOW_RESPONSE_THRESHOLD_MS
        )
        is_slow = (
            result.verdict == HealthVerdict.READY
            and result.ready_for_inference
            and result.response_time_ms >= _SLOW_RESPONSE_THRESHOLD_MS
        )
        is_failure = result.verdict in (
            HealthVerdict.UNREACHABLE, HealthVerdict.UNHEALTHY,
        )

        # Update consecutive counters
        if is_failure:
            self._consecutive_failures += 1
            self._consecutive_slow = 0
        elif is_slow:
            self._consecutive_slow += 1
            self._consecutive_failures = 0
        else:
            # Healthy
            self._consecutive_failures = 0
            self._consecutive_slow = 0

        # Evaluate transitions based on current state
        if self._state == LifecycleState.READY:
            if self._consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                self._root_cause_id = str(uuid.uuid4())
                await self._transition(
                    LifecycleState.UNHEALTHY,
                    "health_monitor",
                    f"{_CONSECUTIVE_FAILURE_LIMIT}_consecutive_failures",
                )
            elif self._consecutive_slow >= _CONSECUTIVE_SLOW_LIMIT:
                await self._transition(
                    LifecycleState.DEGRADED,
                    "health_monitor",
                    f"{_CONSECUTIVE_SLOW_LIMIT}_consecutive_slow",
                )

        elif self._state == LifecycleState.DEGRADED:
            if self._consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                self._root_cause_id = str(uuid.uuid4())
                await self._transition(
                    LifecycleState.UNHEALTHY,
                    "health_monitor",
                    f"{_CONSECUTIVE_FAILURE_LIMIT}_consecutive_failures",
                )
            else:
                # Rolling window: 3 of last 5 healthy -> READY
                healthy_count = sum(
                    1 for r in self._recent_health
                    if r.verdict == HealthVerdict.READY
                    and r.ready_for_inference
                    and r.response_time_ms < _SLOW_RESPONSE_THRESHOLD_MS
                )
                if healthy_count >= _ROLLING_HEALTHY_THRESHOLD:
                    await self._transition(
                        LifecycleState.READY,
                        "health_monitor",
                        "rolling_window_recovery",
                    )

    async def _evaluate_recovery(self) -> None:
        """Evaluate whether to attempt recovery or go TERMINAL.

        Called when in UNHEALTHY state. Checks restart budget.
        """
        now = time.monotonic()
        if self._policy.can_restart(self._restart_timestamps, now):
            self._restart_attempt += 1
            self._restart_timestamps.append(now)
            await self._transition(
                LifecycleState.RECOVERING,
                "auto_recovery",
                "restart_budget_available",
            )
        else:
            await self._transition(
                LifecycleState.TERMINAL,
                "auto_recovery",
                "restart_budget_exhausted",
            )

    # -- Boot gate -----------------------------------------------------------

    async def ensure_ready(self, timeout: float = 120.0) -> str:
        """Boot gate: ensure J-Prime is READY, with Future collapse.

        Concurrent callers share a single boot attempt. Returns a routing
        level string:
          - "LEVEL_0": READY (full J-Prime inference)
          - "LEVEL_1": DEGRADED (J-Prime available but slow)
          - "LEVEL_2": unavailable (TERMINAL / timeout / UNHEALTHY)
        """
        # Fast path: already in a terminal or routable state
        if self._state == LifecycleState.TERMINAL:
            return "LEVEL_2"
        if self._state == LifecycleState.READY:
            return "LEVEL_0"
        if self._state == LifecycleState.DEGRADED:
            return "LEVEL_1"

        async with self._boot_lock:
            # Re-check after acquiring lock
            if self._state == LifecycleState.TERMINAL:
                return "LEVEL_2"
            if self._state == LifecycleState.READY:
                return "LEVEL_0"
            if self._state == LifecycleState.DEGRADED:
                return "LEVEL_1"

            # Collapse concurrent callers onto the same Future
            if self._boot_future is not None and not self._boot_future.done():
                future = self._boot_future
            else:
                loop = asyncio.get_running_loop()
                self._boot_future = loop.create_future()
                # Launch boot sequence in background
                asyncio.ensure_future(self._boot_sequence_wrapper())
                future = self._boot_future

        # Wait for boot to finish (or timeout)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[JprimeLifecycle] ensure_ready timed out after %.1fs", timeout,
            )
            return "LEVEL_2"

    async def _boot_sequence_wrapper(self) -> None:
        """Wrapper that resolves _boot_future with the boot result."""
        try:
            result = await self._boot_sequence()
            if self._boot_future and not self._boot_future.done():
                self._boot_future.set_result(result)
        except Exception as exc:
            logger.error("[JprimeLifecycle] boot sequence failed: %s", exc)
            if self._boot_future and not self._boot_future.done():
                self._boot_future.set_result("LEVEL_2")

    async def _boot_sequence(self) -> str:
        """Internal boot: probe, poll until READY/DEGRADED/TERMINAL.

        Returns routing level string.
        """
        logger.info(
            "[JprimeLifecycle] boot sequence starting -> %s:%d",
            self._host, self._port,
        )

        # Initial probe
        result = await self._do_probe()

        # Poll until terminal condition
        max_iterations = 500  # Safety bound
        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            if self._state == LifecycleState.READY:
                return "LEVEL_0"
            if self._state == LifecycleState.DEGRADED:
                return "LEVEL_1"
            if self._state == LifecycleState.TERMINAL:
                return "LEVEL_2"

            # If UNHEALTHY, evaluate recovery
            if self._state == LifecycleState.UNHEALTHY:
                await self._evaluate_recovery()
                if self._state == LifecycleState.TERMINAL:
                    return "LEVEL_2"

            await asyncio.sleep(_BOOT_POLL_INTERVAL_S)
            result = await self._do_probe()

        logger.error("[JprimeLifecycle] boot sequence exceeded max iterations")
        return "LEVEL_2"

    # -- Continuous health monitor -------------------------------------------

    async def start_health_monitor(self) -> None:
        """Start the background health monitoring loop.

        Safe to call multiple times -- subsequent calls are no-ops.
        """
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.ensure_future(self._health_loop())
        logger.info("[JprimeLifecycle] health monitor started")

    async def _health_loop(self) -> None:
        """Continuous health monitoring loop.

        Probes J-Prime at regular intervals and manages state transitions.
        """
        while True:
            try:
                await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)

                if self._state in (LifecycleState.READY, LifecycleState.DEGRADED):
                    result = await self._probe.check()
                    await self._record_health_result(result)

                elif self._state == LifecycleState.UNHEALTHY:
                    await self._evaluate_recovery()
                    if self._state == LifecycleState.RECOVERING:
                        # Wait for backoff before re-probing
                        backoff = self._policy.backoff_for_attempt(
                            self._restart_attempt,
                        )
                        logger.info(
                            "[JprimeLifecycle] recovering, backoff %.1fs",
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        await self._do_probe()

                elif self._state == LifecycleState.RECOVERING:
                    await self._do_probe()

                elif self._state == LifecycleState.SVC_STARTING:
                    await self._do_probe()

                elif self._state == LifecycleState.TERMINAL:
                    # In TERMINAL, wait for cooldown then optionally retry
                    elapsed = time.monotonic() - self._state_entered_at
                    if elapsed >= self._policy.terminal_cooldown_s:
                        logger.info(
                            "[JprimeLifecycle] terminal cooldown expired, retrying",
                        )
                        self._restart_timestamps.clear()
                        self._restart_attempt = 0
                        await self._transition(
                            LifecycleState.UNKNOWN,
                            "terminal_cooldown",
                            "cooldown_expired",
                        )
                        await self._do_probe()

            except asyncio.CancelledError:
                logger.info("[JprimeLifecycle] health monitor cancelled")
                return
            except Exception:
                logger.error(
                    "[JprimeLifecycle] health loop error", exc_info=True,
                )
                await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)

    async def stop(self) -> None:
        """Stop the background health monitor."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
            logger.info("[JprimeLifecycle] health monitor stopped")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_controller_instance: Optional[JprimeLifecycleController] = None


def get_jprime_lifecycle_controller() -> JprimeLifecycleController:
    """Return the singleton JprimeLifecycleController, creating if needed."""
    global _controller_instance
    if _controller_instance is None:
        _controller_instance = JprimeLifecycleController()
    return _controller_instance
