"""Claude Circuit Breaker — cross-cutting health signal.

Closes the empirical gap surfaced by Move 2 v6: ``ClaudeProvider``'s
internal ``_call_with_backoff`` retry loop absorbs transport failures
(``ConnectTimeout`` / ``SSLWantReadError`` / ``RemoteProtocolError``)
within its 3-attempt window. When all 3 attempts fail on transport
class errors, the exception eventually bubbles up — but the
``FailbackStateMachine`` only sees the failure if the dispatch path
goes through ``_try_primary_then_fallback`` (some paths, like PLAN
generation, call the provider directly via different dispatch seams).

The Circuit Breaker is a CROSS-CUTTING health counter that lives at
the provider boundary itself: every transport-class retry exhaustion
trips a counter; once the threshold is crossed, the breaker OPENS.
While OPEN, the dispatcher consults ``should_allow_request()`` BEFORE
constructing the call and routes directly to fallback if the breaker
says no. After ``recovery_window_s`` of OPEN time, the breaker
transitions to HALF_OPEN to allow one probe; on success the breaker
resets to CLOSED, on failure it returns to OPEN.

State Machine
-------------
::

    CLOSED ---[N consecutive transport exhaustions]--> OPEN
      ^                                                 |
      |                                                 |
      |                                       [recovery_window_s elapses]
      |                                                 |
      |                                                 v
      +-----[probe success]----- HALF_OPEN <-----[allow one probe]
                                  |
                            [probe failure]
                                  |
                                  v
                                 OPEN

Authority Invariant
-------------------
This module imports only from stdlib. No governance, orchestrator, or
provider imports permitted — the breaker is a pure state machine that
the boundary modules (``providers.py``, ``candidate_generator.py``)
consult via ``get_claude_circuit_breaker()``. No reverse dependency.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from typing import Optional


logger = logging.getLogger("Ouroboros.ClaudeCircuitBreaker")


# ---------------------------------------------------------------------------
# Env-driven knobs
# ---------------------------------------------------------------------------


def _failure_threshold() -> int:
    """Consecutive transport-class exhaustions that trip the breaker.

    Default 3 — three back-to-back retry exhaustions on transport
    errors strongly indicates a sustained Claude API outage worth
    routing around. Operator can tune via env."""
    try:
        return int(
            os.environ.get(
                "JARVIS_CLAUDE_CIRCUIT_BREAKER_THRESHOLD", "3",
            )
        )
    except ValueError:
        return 3


def _recovery_window_s() -> float:
    """How long the breaker stays OPEN before allowing a probe.

    Default 900s (15 min) per operator directive — long enough that
    we don't probe a still-flapping endpoint, short enough that
    genuine recovery is observed within one operator workflow."""
    try:
        return float(
            os.environ.get(
                "JARVIS_CLAUDE_CIRCUIT_BREAKER_RECOVERY_S", "900",
            )
        )
    except ValueError:
        return 900.0


def is_enabled() -> bool:
    """Master flag — graduated default-true."""
    raw = os.environ.get(
        "JARVIS_CLAUDE_CIRCUIT_BREAKER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # default-on
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Transport-class detection — string-based to avoid hard httpx dependency
# ---------------------------------------------------------------------------


_TRANSPORT_CLASS_NAMES: frozenset = frozenset({
    "ConnectTimeout",
    "ConnectError",
    "ReadError",
    "ReadTimeout",
    "WriteError",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "ClosedResourceError",
    "ProtocolError",
    "LocalProtocolError",
    "BrokenResourceError",
    "SSLWantReadError",
    "SSLWantWriteError",
    "APITimeoutError",
    "APIConnectionError",
    "APIError",  # outer wrapper — checked last
})


def is_transport_class_exception(exc: BaseException) -> bool:
    """True if ``exc`` (or its __cause__/__context__ chain) is a
    transport-layer failure that warrants tripping the breaker.

    Walks the exception chain like the FailbackStateMachine does,
    matching by class name to avoid hard dependencies on httpx/anyio."""
    seen: set = set()
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth < 8:
        if id(current) in seen:
            break
        seen.add(id(current))
        if type(current).__name__ in _TRANSPORT_CLASS_NAMES:
            return True
        nxt = getattr(current, "__cause__", None)
        if nxt is None:
            nxt = getattr(current, "__context__", None)
        current = nxt
        depth += 1
    return False


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class CircuitState(enum.Enum):
    """Three-state breaker: CLOSED (healthy) / OPEN (sick) / HALF_OPEN (probing)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ClaudeCircuitBreaker:
    """Cross-cutting health gate for Claude transport reliability.

    The breaker is shared globally (one instance per process) so any
    code path invoking the Claude provider can consult the same
    health signal — both the dispatcher path
    (``_try_primary_then_fallback``) and the provider boundary
    (``ClaudeProvider.generate``)."""

    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        recovery_window_s: Optional[float] = None,
    ) -> None:
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_transport_failures: int = 0
        self._tripped_at_monotonic: Optional[float] = None
        self._failure_threshold: int = (
            failure_threshold
            if failure_threshold is not None
            else _failure_threshold()
        )
        self._recovery_window_s: float = (
            recovery_window_s
            if recovery_window_s is not None
            else _recovery_window_s()
        )
        # Successes since last trip — for telemetry / observability.
        self._total_trips: int = 0
        self._total_successes: int = 0
        self._lock = threading.RLock()

    # -- Properties --------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def consecutive_transport_failures(self) -> int:
        with self._lock:
            return self._consecutive_transport_failures

    @property
    def tripped_at_monotonic(self) -> Optional[float]:
        with self._lock:
            return self._tripped_at_monotonic

    @property
    def total_trips(self) -> int:
        with self._lock:
            return self._total_trips

    @property
    def total_successes(self) -> int:
        with self._lock:
            return self._total_successes

    # -- Mutators ---------------------------------------------------------

    def record_transport_exhaustion(
        self, exc_class_name: str = "unknown",
    ) -> None:
        """Record one retry-exhaustion event on a transport-class error.

        Called by ``ClaudeProvider._call_with_backoff`` after its
        internal retry loop has fully exhausted on a transport-class
        exception. The breaker tracks consecutive exhaustions across
        ops; a single op exhausting counts as ONE event, not three.
        """
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                # Probe failed — re-open the breaker, reset its trip clock.
                self._state = CircuitState.OPEN
                self._tripped_at_monotonic = time.monotonic()
                self._total_trips += 1
                logger.warning(
                    "[ClaudeCircuitBreaker] HALF_OPEN probe failed "
                    "(exc=%s) — re-tripping to OPEN",
                    exc_class_name,
                )
                return
            self._consecutive_transport_failures += 1
            if (
                self._state is CircuitState.CLOSED
                and self._consecutive_transport_failures
                >= self._failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._tripped_at_monotonic = time.monotonic()
                self._total_trips += 1
                logger.warning(
                    "[ClaudeCircuitBreaker] TRIPPED (CLOSED -> OPEN) "
                    "after %d consecutive transport exhaustions "
                    "(last=%s) — Claude requests will route to "
                    "fallback for next %.0fs",
                    self._consecutive_transport_failures,
                    exc_class_name,
                    self._recovery_window_s,
                )

    def record_success(self) -> None:
        """Record a successful Claude call. Resets consecutive-failure
        counter and, if HALF_OPEN, transitions back to CLOSED.

        Called by ``ClaudeProvider.generate`` on the success path.
        Idempotent — multiple successes in a row don't accumulate."""
        with self._lock:
            self._total_successes += 1
            self._consecutive_transport_failures = 0
            if self._state is not CircuitState.CLOSED:
                logger.info(
                    "[ClaudeCircuitBreaker] %s -> CLOSED (probe "
                    "succeeded after %d total trips, %d successes)",
                    self._state.name,
                    self._total_trips,
                    self._total_successes,
                )
                self._state = CircuitState.CLOSED
                self._tripped_at_monotonic = None

    def record_non_transport_failure(self) -> None:
        """Record a non-transport failure (content failure, validation,
        etc.). Does NOT trip the breaker — those are upstream signals
        about Claude's *behavior* on healthy infra, not infra health.
        Resets the consecutive-transport counter so a single transport
        flap interspersed with content failures doesn't accumulate."""
        with self._lock:
            self._consecutive_transport_failures = 0

    def should_allow_request(self) -> bool:
        """Pre-call gate: True if a Claude request should be attempted.

        Side-effect: if state is OPEN and the recovery window has
        elapsed, transitions to HALF_OPEN and returns True for ONE
        probe. Subsequent calls while still HALF_OPEN return False
        until the probe resolves (record_success / record_transport
        exhaustion)."""
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                if self._tripped_at_monotonic is None:
                    # Defensive: shouldn't happen, but recover gracefully.
                    self._state = CircuitState.HALF_OPEN
                    return True
                elapsed = time.monotonic() - self._tripped_at_monotonic
                if elapsed >= self._recovery_window_s:
                    self._state = CircuitState.HALF_OPEN
                    logger.info(
                        "[ClaudeCircuitBreaker] OPEN -> HALF_OPEN "
                        "(recovery window %.0fs elapsed) — allowing "
                        "one probe",
                        self._recovery_window_s,
                    )
                    return True
                return False
            # HALF_OPEN: only one probe in flight; subsequent callers
            # see False until the probe resolves. The probe IS in flight
            # for whichever caller acquired the True earlier.
            return False

    def reset(self) -> None:
        """Test hook — force the breaker back to CLOSED with zero counters."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_transport_failures = 0
            self._tripped_at_monotonic = None

    def snapshot(self) -> dict:
        """Read-only view of the breaker for telemetry / dashboards."""
        with self._lock:
            return {
                "state": self._state.value,
                "consecutive_transport_failures":
                    self._consecutive_transport_failures,
                "tripped_at_monotonic": self._tripped_at_monotonic,
                "failure_threshold": self._failure_threshold,
                "recovery_window_s": self._recovery_window_s,
                "total_trips": self._total_trips,
                "total_successes": self._total_successes,
            }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_singleton: Optional[ClaudeCircuitBreaker] = None
_singleton_lock = threading.Lock()


def get_claude_circuit_breaker() -> ClaudeCircuitBreaker:
    """Get (or lazily construct) the process-wide breaker singleton.

    All call sites consult the same instance so the health signal is
    consistent across the dispatcher and provider boundaries."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ClaudeCircuitBreaker()
    return _singleton


def reset_singleton_for_tests() -> None:
    """Test isolation — drop the singleton so the next call rebuilds
    a fresh breaker with current env. Never call from production."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "CircuitState",
    "ClaudeCircuitBreaker",
    "get_claude_circuit_breaker",
    "is_enabled",
    "is_transport_class_exception",
    "reset_singleton_for_tests",
]
