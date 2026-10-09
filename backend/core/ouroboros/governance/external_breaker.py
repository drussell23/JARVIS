"""Circuit breakers for EXTERNAL dependencies the tool loop calls out to.

MCP servers and the web are services O+V does not run. A hung server, a
dead socket or a malformed payload must cost the op one bounded tool error,
never a stalled round, and must stop being retried by every op that follows.

Why not ``governance.circuit_breaker``: that breaker consumes the provider
retry classifier's decisions, and its structural trips escalate to the
session-wide provider-exhaustion breaker. An MCP server going away is not
the model lanes being exhausted, and wiring the one into the other would
park generation over a missing GitHub token.

One breaker per KEY (``mcp:<server>``, ``web:search``, ``web:fetch:<host>``),
three states:

* CLOSED: calls pass; consecutive failures are counted.
* OPEN: after ``JARVIS_EXTERNAL_BREAKER_THRESHOLD`` consecutive failures,
  calls fail fast with a structured reason for a cooldown that doubles on
  every re-trip (``JARVIS_EXTERNAL_BREAKER_COOLDOWN_S`` up to
  ``JARVIS_EXTERNAL_BREAKER_MAX_COOLDOWN_S``).
* HALF_OPEN: once the cooldown lapses, ONE trial call passes; its outcome
  closes or re-opens the breaker. Concurrent callers keep failing fast until
  it resolves, so a recovering server is not stampeded.

:func:`guard` is the one way to call through a breaker: it bounds the call
with a timeout, classifies timeouts, exceptions and malformed results as
failures, and returns an :class:`Outcome` instead of raising, so the caller
always gets something to hand the model. ``CancelledError`` propagates.
Thread-safe; NEVER raises otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.ExternalBreaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


def _env(name: str, default: float, lo: float) -> float:
    try:
        return max(lo, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def threshold() -> int:
    return int(_env("JARVIS_EXTERNAL_BREAKER_THRESHOLD", 3, 1))


def base_cooldown_s() -> float:
    return _env("JARVIS_EXTERNAL_BREAKER_COOLDOWN_S", 30.0, 0.1)


def max_cooldown_s() -> float:
    return _env("JARVIS_EXTERNAL_BREAKER_MAX_COOLDOWN_S", 600.0, 0.1)


@dataclass(frozen=True)
class Outcome:
    """What a guarded call produced. ``ok`` with ``value``, or a reason."""
    ok: bool
    value: Any = None
    reason: str = ""          # "", "timeout", "open", "malformed", "error: …"
    state: str = CLOSED


class ExternalBreaker:
    """One key's breaker. Use through :func:`guard` or :func:`breaker_for`."""

    def __init__(self, key: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.key = key
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._trips = 0
        self._open_until = 0.0
        self._trial_in_flight = False
        self.last_reason = ""

    @property
    def state(self) -> str:
        with self._lock:
            return self._effective_state_locked()

    def _effective_state_locked(self) -> str:
        if self._state == OPEN and self._clock() >= self._open_until:
            return HALF_OPEN
        return self._state

    def admit(self) -> bool:
        """May a call go through now? In HALF_OPEN, exactly one may."""
        with self._lock:
            state = self._effective_state_locked()
            if state == CLOSED:
                return True
            if state == HALF_OPEN and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state != CLOSED:
                logger.info("[ExternalBreaker] %s recovered", self.key)
            self._state, self._failures, self._trips = CLOSED, 0, 0
            self._trial_in_flight = False
            self.last_reason = ""

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self.last_reason = reason
            trial = self._trial_in_flight
            self._trial_in_flight = False
            self._failures += 1
            if trial or self._failures >= threshold():
                self._trips += 1
                cooldown = min(max_cooldown_s(),
                               base_cooldown_s() * (2 ** (self._trips - 1)))
                self._state = OPEN
                self._open_until = self._clock() + cooldown
                logger.warning("[ExternalBreaker] %s OPEN for %.0fs after %s",
                               self.key, cooldown, reason)

    def release_trial(self) -> None:
        """A trial ended with no verdict (cancelled). Without this the
        half-open slot stays taken and the breaker refuses every call
        forever."""
        with self._lock:
            self._trial_in_flight = False

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"key": self.key, "state": self._effective_state_locked(),
                    "failures": self._failures, "trips": self._trips,
                    "reason": self.last_reason,
                    "retry_in_s": max(0.0, round(self._open_until - self._clock(), 1))}


_REGISTRY: Dict[str, ExternalBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def breaker_for(key: str) -> ExternalBreaker:
    """The process-wide breaker for *key*, created on first use."""
    with _REGISTRY_LOCK:
        brk = _REGISTRY.get(key)
        if brk is None:
            brk = _REGISTRY[key] = ExternalBreaker(key)
        return brk


def snapshot_all() -> Dict[str, Dict[str, Any]]:
    """Every breaker's state, for health surfaces."""
    with _REGISTRY_LOCK:
        breakers = list(_REGISTRY.values())
    return {b.key: b.snapshot() for b in breakers}


async def guard(key: str, call: Callable[[], Awaitable[Any]], *,
                timeout: float,
                valid: Optional[Callable[[Any], bool]] = None) -> Outcome:
    """Run *call* through *key*'s breaker, bounded by *timeout*.

    A result *valid* rejects counts as a failure (``malformed``): a server
    that answers garbage is as broken as one that does not answer. Never
    raises except ``CancelledError``, which is a shutdown and must propagate.
    """
    brk = breaker_for(key)
    if not brk.admit():
        snap = brk.snapshot()
        return Outcome(False, reason=f"open: {snap['reason'] or 'recent failures'}"
                       f" (retry in {snap['retry_in_s']:.0f}s)", state=OPEN)
    try:
        value = await asyncio.wait_for(call(), timeout=max(0.1, float(timeout)))
    except asyncio.CancelledError:
        brk.release_trial()
        raise
    except asyncio.TimeoutError:
        brk.record_failure("timeout")
        return Outcome(False, reason="timeout", state=brk.state)
    except Exception as exc:  # noqa: BLE001 — every fault is an outcome
        reason = f"error: {type(exc).__name__}: {exc}"[:240]
        brk.record_failure(reason)
        return Outcome(False, reason=reason, state=brk.state)
    try:
        if valid is not None and not valid(value):
            brk.record_failure("malformed")
            return Outcome(False, value=value, reason="malformed", state=brk.state)
    except Exception:  # noqa: BLE001 — a validator that throws calls it malformed
        brk.record_failure("malformed")
        return Outcome(False, value=value, reason="malformed", state=brk.state)
    brk.record_success()
    return Outcome(True, value=value, state=CLOSED)


__all__ = ["CLOSED", "ExternalBreaker", "HALF_OPEN", "OPEN", "Outcome",
           "breaker_for", "guard", "snapshot_all"]
