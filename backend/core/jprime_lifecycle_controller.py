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
