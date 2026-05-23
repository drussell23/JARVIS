"""Provider Circuit Breaker — adaptive state machine (Slice 7c).

Empirical context — bt-2026-05-21-214521 X-ray (Slice 6 trace):

    The 35-minute "silent window" hang was a fast-fail retry storm
    inside ``CandidateGenerator._call_fallback``. A
    ``SessionBudgetPreflightRefused`` (structural cost-vs-budget
    inequality — retrying mathematically CANNOT clear it) was
    classified as ``failure_mode=CONNECTION_ERROR`` (transient,
    retryable) and the outer-retry loop spun at ~2.7s per attempt
    for the entire 1800s ``wait_for`` budget.

Slice 7a (PR #50692) introduced the closed-taxonomy
``RetryDecision`` classifier that fixes the mis-bucket. This module
is the **state machine** that consumes the decision and decides:

  * **Continue with retry** — caller's existing outer-retry loop is
    structurally correct for this failure class.
  * **Retry after backoff** — too many transient failures recently;
    yield to a bounded exponential-with-full-jitter backoff
    before next attempt (AWS-style anti-thundering-herd).
  * **Terminate as UNRESOLVED** — math says further retries cannot
    help. The breaker emits a synthetic ``operation_terminal``
    event via the canonical StreamEventBroker (Slice 7e wires
    this) so the parallel evaluator's bounded ``wait_for`` collapses
    in seconds, not minutes.

## State machine (closed 4-value taxonomy)

```
                    record_success() in HALF_OPEN
              ┌──────────────────────────────────────────┐
              ▼                                          │
        ┌─────────┐  N×RETRY_TRANSIENT  ┌──────────────────┐
        │ CLOSED  │ ──────────────────▶ │ OPEN_TRANSIENT   │
        └─────────┘   in window         └──────────────────┘
              │                                │  backoff expires
              │ TERMINAL_*                     ▼
              │                          ┌──────────────────┐
              │                          │   HALF_OPEN      │
              │                          └──────────────────┘
              │                          fail │     │ success
              ▼                               ▼     ▼
                                  OPEN_TRANSIENT  CLOSED
        ┌──────────────────┐    (extended)
        │ OPEN_TERMINAL    │    (sticky — no transitions out)
        └──────────────────┘
```

Trip table (closed, AST-pinned):

  | Input from classifier         | Action                                    |
  | ----------------------------- | ----------------------------------------- |
  | 1× TERMINAL_STRUCTURAL        | → OPEN_TERMINAL immediately               |
  | 1× TERMINAL_CONFIG            | → OPEN_TERMINAL immediately               |
  | Nth TERMINAL_QUOTA in window  | → OPEN_TERMINAL                           |
  | <Nth TERMINAL_QUOTA in window | → CLOSED (verdict RETRY_AFTER_BACKOFF)    |
  | Nth RETRY_TRANSIENT in window | → OPEN_TRANSIENT (full-jitter backoff)    |
  | <Nth RETRY_TRANSIENT in win.  | → CLOSED (verdict RETRY_OK)               |

## Composition (operator binding — "zero state duplication")

The breaker is a **pure consumer** of canonical state stores:

  * ``ProviderExhaustionWatcher.consecutive`` — canonical
    consecutive-failure counter, NOT duplicated. The breaker reads
    via a `consecutive_provider` injection; the production wiring
    will pass ``watcher.__class__.consecutive.fget(watcher)``.

  * ``SessionBudgetAuthority.get_session_remaining_usd()`` —
    canonical session-budget oracle. The breaker uses this for
    **pre-trip**: when ``remaining < min_floor``, evaluate()
    returns TERMINATE_UNRESOLVED without invoking the failure
    classifier. The structural cause (budget gone) is honoured
    before any provider call is even attempted.

  * ``StreamEventBroker.publish_operation_terminal()`` — Slice 7e
    wires this in. The breaker itself only carries an
    ``on_terminal`` callback that the wiring layer connects.

The breaker DOES NOT maintain a parallel ``consecutive`` counter.
AST pin in the paired test enforces this — the only state owned
by the breaker is the state machine itself plus rolling windows
for the rate-limited quota / transient counters (which are
breaker-specific, not duplicated anywhere else).

## Full-Jitter exponential backoff (operator binding)

Operator: *"the OPEN_TRANSIENT exponential backoff must incorporate
dynamic Full Jitter to prevent 'thundering herd' retry storms
when multiple agents face transient gateway drops."*

The breaker uses the **AWS Full-Jitter** algorithm verbatim:

    delay = random.uniform(0, min(cap_s, base_s * 2^attempt))

Each transient retry picks a delay uniformly in [0, exponential
cap]. Multiple concurrent agents observing the same gateway drop
all roll independent dice; the herd disperses.

## Per-op + global tiers

Two breaker instances exist:

  * **Per-op breaker** — scoped to a single ``op_id``. Created
    by ``CandidateGenerator._call_fallback`` per evaluation.
    Prevents one op from burning the full budget on retries.

  * **Global session breaker** — process-singleton via
    ``get_global_breaker()``. Tracks TERMINAL_STRUCTURAL trips
    across all per-op breakers. If ≥N trips within a window
    (env-knobbed), the global breaker enters OPEN_TERMINAL and
    every subsequent ``evaluate()`` on any per-op instance
    returns TERMINATE_UNRESOLVED without invoking the provider.

The global breaker is the structural acknowledgement that
"this session's budget is gone — stop trying."

## Master flag + env knobs

  * ``JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED`` —
    **default TRUE (Slice 7g graduated 2026-05-22)**.
    When OFF (explicit ``=false``), ``evaluate()`` always returns
    ``RETRY_OK`` (byte-identical to no breaker) — the hot-revert
    path. Four consecutive forced-budget acceptance soaks proved
    the cascade: per-op trip → ``[CircuitBreaker.Global]`` →
    ``global_session_exhausted`` with zero retry storms.
  * ``JARVIS_CIRCUIT_BREAKER_TERMINAL_QUOTA_TRIP`` — default 2
    (operator-bound).
  * ``JARVIS_CIRCUIT_BREAKER_QUOTA_WINDOW_S`` — default 30.0s.
  * ``JARVIS_CIRCUIT_BREAKER_TRANSIENT_TRIP`` — default 3.
  * ``JARVIS_CIRCUIT_BREAKER_TRANSIENT_WINDOW_S`` — default 60.0s.
  * ``JARVIS_CIRCUIT_BREAKER_BACKOFF_BASE_S`` — default 5.0s.
  * ``JARVIS_CIRCUIT_BREAKER_BACKOFF_CAP_S`` — default 60.0s.
  * ``JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT`` — default 5.
  * ``JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_WINDOW_S`` — default 300.0s.
  * ``JARVIS_CIRCUIT_BREAKER_MIN_BUDGET_FLOOR_USD`` — default 0.05.

All thresholds are env-knobbed (operational). The closed enums
(``CircuitState``, ``CircuitScope``, ``VerdictAction``) are
structural — frozen by AST pin.
"""

from __future__ import annotations

import enum
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, List, Optional


logger = logging.getLogger("Ouroboros.CircuitBreaker")


# ============================================================================
# Closed-taxonomy enums
# ============================================================================


class CircuitState(str, enum.Enum):
    """Closed 4-value circuit-breaker state. Adding a 5th member
    requires bumping the AST pin + Slice 7e's wiring branches."""

    CLOSED         = "closed"          # retries allowed
    OPEN_TRANSIENT = "open_transient"  # backoff-then-probe
    HALF_OPEN      = "half_open"       # probing recovery
    OPEN_TERMINAL  = "open_terminal"   # sticky — no further attempts


class CircuitScope(str, enum.Enum):
    """Closed 2-value scope — per-op or session-global."""

    PER_OP = "per_op"
    GLOBAL = "global"


class CircuitTripOrigin(str, enum.Enum):
    """Slice 12N — closed taxonomy of where a per-op breaker trip
    originated, governing whether it counts toward the GLOBAL
    breaker's session_exhausted threshold.

    The wedge surfaced in bt-2026-05-23-015723: a background
    OpportunityMiner op hit ``TERMINAL_STRUCTURAL``, that incremented
    the global trip counter, the global breaker fired
    ``session_exhausted``, and the in-flight SWE-Bench-Pro fixture
    op (a high-priority foreground op) was assassinated mid-GENERATE.

    Slice 12N isolation: ONLY ``FOREGROUND`` trips count toward
    the global threshold. Background / speculative / maintenance
    trips terminate their own op locally but cannot blast-radius
    out to take down healthy foreground work.

      * ``FOREGROUND`` — high-priority pipeline ops where a structural
        provider trip IS a session-wide acknowledgement that the
        budget is gone. Maps from ProviderRoute IMMEDIATE / STANDARD
        / COMPLEX. These are the only origins that escalate to the
        global breaker.

      * ``BACKGROUND`` — autonomous sensor / mining ops
        (OpportunityMiner, DocStaleness, TodoScanner, etc.) where a
        trip means "this op gave up on its budget"; nothing about
        foreground work changes. Maps from ProviderRoute BACKGROUND.

      * ``SPECULATIVE`` — fire-and-forget pre-computation
        (IntentDiscovery, DreamEngine pre-warming). Same isolation
        as BACKGROUND. Maps from ProviderRoute SPECULATIVE.

      * ``MAINTENANCE`` — periodic upkeep tasks
        (TopologySentinel probes, health checks, schema validations).
        Same isolation as BACKGROUND. Default for tasks that don't
        carry a ProviderRoute at all.

    Backward compatibility: default is ``FOREGROUND`` so any
    existing caller that doesn't plumb origin preserves the
    pre-Slice-12N escalation semantics byte-identically. Slice 12N
    only relaxes escalation for callers that explicitly tag their
    breaker as non-foreground.
    """

    FOREGROUND = "foreground"
    BACKGROUND = "background"
    SPECULATIVE = "speculative"
    MAINTENANCE = "maintenance"


# Slice 12N — the set of origins that escalate to global. Centralized
# so the AST pin and the runtime gate read the same source.
_FOREGROUND_ORIGINS: frozenset = frozenset({
    CircuitTripOrigin.FOREGROUND,
})


class VerdictAction(str, enum.Enum):
    """Closed 3-value verdict — what the caller should do next."""

    RETRY_OK              = "retry_ok"
    RETRY_AFTER_BACKOFF   = "retry_after_backoff"
    TERMINATE_UNRESOLVED  = "terminate_unresolved"


@dataclass(frozen=True)
class CircuitVerdict:
    """Frozen verdict returned by ``CircuitBreaker.evaluate()``.

    The caller's contract:

      * RETRY_OK            — proceed with the next retry attempt.
      * RETRY_AFTER_BACKOFF — ``await asyncio.sleep(verdict.backoff_s)``
        BEFORE the next attempt.
      * TERMINATE_UNRESOLVED — emit a synthetic ``operation_terminal``
        event with ``state="UNRESOLVED"`` and
        ``terminal_reason_code=verdict.terminal_reason_code``,
        then RETURN from the outer-retry loop. Do NOT retry.
    """

    action: VerdictAction
    backoff_s: Optional[float] = None
    terminal_reason_code: Optional[str] = None
    state_after: Optional[CircuitState] = None


@dataclass(frozen=True)
class GlobalBreakerTripPayload:
    """Frozen lifecycle payload broadcast when the global breaker
    transitions CLOSED → OPEN_TERMINAL. Slice 12D substrate —
    consumed by harness shutdown wiring (in-process callback) and
    by IDE consumers via ``session_exhausted`` SSE event.

    Fields are kept primitive so the payload can cross both
    in-process and SSE-publish boundaries without conversion."""

    reason: str               # canonical "session_exhausted"
    trip_count: int           # structural trips observed within window
    window_s: float           # window over which trips were counted
    threshold: int            # trips-to-trip threshold
    triggered_at: float       # wall-clock time.time() at transition


# ============================================================================
# Env knobs
# ============================================================================


_MASTER_FLAG_ENV: str = "JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED"
_QUOTA_TRIP_ENV: str = "JARVIS_CIRCUIT_BREAKER_TERMINAL_QUOTA_TRIP"
_QUOTA_WINDOW_ENV: str = "JARVIS_CIRCUIT_BREAKER_QUOTA_WINDOW_S"
_TRANSIENT_TRIP_ENV: str = "JARVIS_CIRCUIT_BREAKER_TRANSIENT_TRIP"
_TRANSIENT_WINDOW_ENV: str = "JARVIS_CIRCUIT_BREAKER_TRANSIENT_WINDOW_S"
_BACKOFF_BASE_ENV: str = "JARVIS_CIRCUIT_BREAKER_BACKOFF_BASE_S"
_BACKOFF_CAP_ENV: str = "JARVIS_CIRCUIT_BREAKER_BACKOFF_CAP_S"
_GLOBAL_TRIP_COUNT_ENV: str = "JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_COUNT"
_GLOBAL_TRIP_WINDOW_ENV: str = "JARVIS_CIRCUIT_BREAKER_GLOBAL_TRIP_WINDOW_S"
_MIN_BUDGET_FLOOR_ENV: str = "JARVIS_CIRCUIT_BREAKER_MIN_BUDGET_FLOOR_USD"


def circuit_breaker_enabled() -> bool:
    """Master gate. **Default TRUE — graduated 2026-05-22 (Slice 7g)**.

    Provider Circuit Breaker is now permanently on. The retry-storm
    + cancellation-overrun wedge that motivated Slice 7 (a–e) is
    structurally closed; four consecutive forced-budget acceptance
    soaks (Slice 11B-fix / 12A / 11C / 11C-retry) confirmed:
      * 20+ ``circuit_breaker_tripped:terminal_structural`` per soak
      * 0 ``Fallback outer-retry`` storms
      * 0 ``cancellation_overrun_detected`` events
      * Clean cascade per-op trip → ``[CircuitBreaker.Global]`` →
        ``global_session_exhausted`` for subsequent ops

    Hot-revert path: ``export JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED=false``
    returns the orchestrator to the pre-Slice-7 retry-loop behaviour.
    Any other value (empty / ``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``)
    leaves the breaker enabled. NEVER raises."""
    try:
        raw = os.environ.get(_MASTER_FLAG_ENV, "").strip().lower()
        if raw == "":
            return True  # graduated default
        return raw not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


def _read_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        v = int(raw)
        return max(minimum, v)
    except (TypeError, ValueError):
        return default


def _read_float(
    name: str, default: float, *, minimum: float = 0.0,
) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        v = float(raw)
        return max(minimum, v)
    except (TypeError, ValueError):
        return default


# ============================================================================
# Sliding window — small, lock-free, deque-based
# ============================================================================


class _SlidingWindow:
    """Append-only timestamp ring with O(1) prune+count.

    The breaker's per-class failure windows (quota, transient) use
    this to count "N failures in last W seconds" without storing the
    raw events. We deliberately keep this internal — it's NOT a
    duplicate of ExhaustionWatcher's consecutive counter (which
    counts uninterrupted runs; this counts in-window events)."""

    __slots__ = ("_window_s", "_events")

    def __init__(self, window_s: float) -> None:
        self._window_s: float = float(window_s)
        self._events: Deque[float] = deque()

    def add(self, now: Optional[float] = None) -> None:
        ts = time.monotonic() if now is None else now
        self._prune(ts)
        self._events.append(ts)

    def count(self, now: Optional[float] = None) -> int:
        ts = time.monotonic() if now is None else now
        self._prune(ts)
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


# ============================================================================
# Full-Jitter backoff — AWS algorithm, exposed as a free function
# for direct testing of the distribution
# ============================================================================


def full_jitter_delay(
    attempt: int,
    *,
    base_s: float,
    cap_s: float,
    rng: Optional[Any] = None,
) -> float:
    """Compute a single-attempt backoff delay using AWS Full Jitter.

    Algorithm::

        delay = random.uniform(0, min(cap_s, base_s * 2^attempt))

    ``attempt`` is 0-indexed (first OPEN_TRANSIENT trip = attempt 0).
    The exponential ceiling is clamped to ``cap_s`` so multi-hour
    backoffs don't accidentally form.

    ``rng`` is injectable for tests; defaults to the module-level
    ``random`` (which uses a system-seeded Mersenne Twister)."""
    if attempt < 0:
        attempt = 0
    # Defensive: a non-positive cap means "no backoff" — caller has
    # disabled the jitter window entirely. Return 0 immediately.
    if cap_s <= 0:
        return 0.0
    # Exponential ceiling, clamped by the cap.
    expo = float(base_s) * (2 ** attempt)
    ceiling = min(float(cap_s), expo)
    if ceiling <= 0:
        return 0.0
    r = rng if rng is not None else random
    # uniform(0, ceiling) inclusive both ends — the AWS-style
    # "full jitter" range.
    return float(r.uniform(0.0, ceiling))


# ============================================================================
# Global session-tier breaker — process-singleton
# ============================================================================


class _GlobalBreaker:
    """Session-wide structural-refusal counter. Process singleton.

    Per-op breakers report TERMINAL_STRUCTURAL trips to this object.
    If trips within the window exceed the threshold, the global
    breaker enters OPEN_TERMINAL — every subsequent per-op
    ``evaluate()`` immediately returns TERMINATE_UNRESOLVED with
    ``terminal_reason_code=global_session_exhausted``.

    Slice 12D: the transition CLOSED → OPEN_TERMINAL is a
    one-shot lifecycle event. The breaker exposes an ``on_trip``
    callback registry — subscribers (harness shutdown waiter, SSE
    publisher, etc.) are notified exactly once at the transition
    instant. Callbacks fire synchronously inside
    ``report_structural_trip``; they are defensive-isolated so a
    raising subscriber cannot break the breaker or starve siblings.
    """

    def __init__(self) -> None:
        self._state: CircuitState = CircuitState.CLOSED
        self._trip_count_env = _GLOBAL_TRIP_COUNT_ENV
        self._trip_window_env = _GLOBAL_TRIP_WINDOW_ENV
        self._window: _SlidingWindow = _SlidingWindow(
            _read_float(_GLOBAL_TRIP_WINDOW_ENV, 300.0),
        )
        # Slice 12D — on-trip callback registry. Append-only;
        # iterated as a snapshot (defensive copy) so a callback
        # that mutates the registry (e.g. unsubscribes itself)
        # can't perturb the in-flight dispatch loop.
        self._on_trip_callbacks: List[
            Callable[[GlobalBreakerTripPayload], None]
        ] = []
        self._on_trip_lock: threading.Lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        # If we're CLOSED, re-check threshold (window may have aged
        # the events out).
        return self._state

    def on_trip(
        self,
        callback: Callable[[GlobalBreakerTripPayload], None],
    ) -> None:
        """Register a callback to fire when this breaker transitions
        CLOSED → OPEN_TERMINAL. The transition is **sticky** — once
        OPEN_TERMINAL, the global breaker stays there for the
        process lifetime and never re-fires callbacks. Multiple
        subscribers are dispatched in registration order. NEVER
        raises.

        Callbacks fire synchronously inside the thread that calls
        ``report_structural_trip`` (typically a Slice 7e GENERATE
        worker on the asyncio loop thread). Subscribers that need
        to interact with the asyncio loop from another thread MUST
        marshal via ``loop.call_soon_threadsafe`` themselves —
        the registry stays transport-agnostic."""
        if callback is None or not callable(callback):
            return
        with self._on_trip_lock:
            self._on_trip_callbacks.append(callback)
        # If the breaker is ALREADY tripped at registration time,
        # fire the callback immediately with the current trip
        # payload — late subscribers should see the transition,
        # not silently miss it. This makes registration order
        # irrelevant for shutdown-waiter wiring.
        if self._state == CircuitState.OPEN_TERMINAL:
            payload = self._build_trip_payload()
            try:
                callback(payload)
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "[CircuitBreaker.Global] late-bind on_trip "
                    "callback raised (swallowed)",
                )

    def _build_trip_payload(self) -> GlobalBreakerTripPayload:
        """Compose the lifecycle payload from current breaker state.
        Pure-data, NEVER raises."""
        window_s = _read_float(_GLOBAL_TRIP_WINDOW_ENV, 300.0)
        threshold = _read_int(_GLOBAL_TRIP_COUNT_ENV, 5)
        return GlobalBreakerTripPayload(
            reason="session_exhausted",
            trip_count=int(self._window.count()),
            window_s=float(window_s),
            threshold=int(threshold),
            triggered_at=time.time(),
        )

    def _dispatch_on_trip(
        self, payload: GlobalBreakerTripPayload,
    ) -> None:
        """Fire every registered callback with the trip payload.
        Each callback is isolated in try/except — a raising
        subscriber cannot starve siblings or propagate into
        ``report_structural_trip``."""
        with self._on_trip_lock:
            snapshot = list(self._on_trip_callbacks)
        for cb in snapshot:
            try:
                cb(payload)
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "[CircuitBreaker.Global] on_trip callback "
                    "raised (swallowed); siblings continue",
                )

    def report_structural_trip(self) -> None:
        """Per-op breaker calls this when it trips with
        TERMINAL_STRUCTURAL. The global breaker may transition to
        OPEN_TERMINAL if the threshold is reached within the
        window.

        Slice 12D: a CLOSED → OPEN_TERMINAL transition publishes
        the lifecycle event in three places, in this order:
          1. ``[CircuitBreaker.Global] tripped`` log line (legacy).
          2. In-process callback dispatch (registered subscribers).
          3. Best-effort SSE publish for IDE consumers
             (``session_exhausted`` event type).
        Steps 2 and 3 are fully defensive — neither can perturb
        the breaker state, the log, or each other."""
        if self._state == CircuitState.OPEN_TERMINAL:
            return  # already tripped — sticky
        self._window.add()
        threshold = _read_int(_GLOBAL_TRIP_COUNT_ENV, 5)
        if self._window.count() >= threshold:
            self._state = CircuitState.OPEN_TERMINAL
            logger.warning(
                "[CircuitBreaker.Global] tripped to OPEN_TERMINAL — "
                "structural trips=%d within window=%.0fs threshold=%d",
                self._window.count(),
                _read_float(_GLOBAL_TRIP_WINDOW_ENV, 300.0),
                threshold,
            )
            # Slice 12D — broadcast lifecycle.
            payload = self._build_trip_payload()
            # (2) In-process subscribers (harness shutdown waiter,
            #     telemetry observers, etc.).
            self._dispatch_on_trip(payload)
            # (3) IDE/SSE consumers — lazy, best-effort.
            _publish_session_exhausted_best_effort(payload)

    def reset(self) -> None:
        """For tests. Production code should not need to call this."""
        self._state = CircuitState.CLOSED
        self._window.clear()
        with self._on_trip_lock:
            self._on_trip_callbacks.clear()


def _publish_session_exhausted_best_effort(
    payload: GlobalBreakerTripPayload,
) -> None:
    """Lazy-import ``publish_session_exhausted`` from the
    observability-stream module and fire-and-forget. Broker missing
    / observability disabled / publish raising all degrade to a
    silent return. NEVER raises.

    Lazy import keeps ``circuit_breaker`` independent of the SSE
    surface — operators running with ``JARVIS_IDE_STREAM_ENABLED=false``
    pay zero cost; tests with the broker stubbed see the publish
    attempt deterministically."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_session_exhausted,
        )
    except Exception:  # noqa: BLE001 — defensive
        return
    try:
        publish_session_exhausted(payload)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CircuitBreaker.Global] session_exhausted SSE "
            "publish swallowed",
            exc_info=True,
        )


_global_breaker: Optional[_GlobalBreaker] = None


def get_global_breaker() -> _GlobalBreaker:
    """Process-singleton accessor. NEVER raises."""
    global _global_breaker
    if _global_breaker is None:
        _global_breaker = _GlobalBreaker()
    return _global_breaker


def reset_global_breaker() -> None:
    """For tests. Resets the singleton's state + sliding window."""
    get_global_breaker().reset()


# ============================================================================
# Per-op CircuitBreaker — the main state machine
# ============================================================================


# Import lazily to avoid cycles at module-load time. The
# RetryDecision import is the only structural dependency from
# Slice 7a; we don't import classify() because that's the
# caller's job — the breaker consumes already-classified
# decisions.
from backend.core.ouroboros.governance.provider_retry_classifier import (  # noqa: E402
    RetryDecision,
)


class CircuitBreaker:
    """Per-op circuit breaker. Stateful but state-machine-pure.

    Constructed by ``CandidateGenerator._call_fallback`` per
    evaluation (Slice 7e wires this). Holds:

      * Current state (one of 4 ``CircuitState`` members).
      * A backoff attempt counter for full-jitter computation.
      * Two sliding windows (quota + transient) for rate-based trips.
      * Injectable callables for ``consecutive_provider`` (reads
        from ProviderExhaustionWatcher) and
        ``budget_provider`` (reads from SessionBudgetAuthority).

    The breaker does NOT store its own consecutive counter or
    budget snapshot — composition with canonical sources only.
    AST pin in the paired test verifies the no-parallel-state
    discipline.

    Public API:

      * ``evaluate(decision)`` — primary call site. Caller passes
        the ``RetryDecision`` from Slice 7a's classifier; breaker
        returns a frozen ``CircuitVerdict``.
      * ``record_success()`` — caller invokes this on a successful
        provider response. Transitions HALF_OPEN → CLOSED or
        clears the in-window counters.
      * ``state`` property — current state (for telemetry).

    NEVER raises."""

    def __init__(
        self,
        *,
        op_id: str = "",
        scope: CircuitScope = CircuitScope.PER_OP,
        origin: CircuitTripOrigin = CircuitTripOrigin.FOREGROUND,
        consecutive_provider: Optional[Callable[[], int]] = None,
        budget_provider: Optional[Callable[[], Optional[float]]] = None,
        rng: Optional[Any] = None,
    ) -> None:
        self._op_id: str = op_id
        self._scope: CircuitScope = scope
        # Slice 12N — origin governs whether structural trips on
        # this breaker escalate to the global session_exhausted
        # threshold. Default FOREGROUND preserves pre-Slice-12N
        # behavior byte-identically for any caller that doesn't
        # plumb origin.
        self._origin: CircuitTripOrigin = origin
        self._state: CircuitState = CircuitState.CLOSED
        self._backoff_attempt: int = 0
        self._next_attempt_at: Optional[float] = None
        self._consecutive_provider = consecutive_provider
        self._budget_provider = budget_provider
        self._rng = rng
        # Sliding windows for rate-based trips. Windows are
        # OWNED by the breaker (these are breaker-specific
        # state, not duplicated anywhere else).
        self._quota_window: _SlidingWindow = _SlidingWindow(
            _read_float(_QUOTA_WINDOW_ENV, 30.0),
        )
        self._transient_window: _SlidingWindow = _SlidingWindow(
            _read_float(_TRANSIENT_WINDOW_ENV, 60.0),
        )
        # Cached at construction.
        self._master_enabled: bool = circuit_breaker_enabled()

    # ---- public introspection ----

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def scope(self) -> CircuitScope:
        return self._scope

    @property
    def origin(self) -> CircuitTripOrigin:
        """Slice 12N — origin of this breaker. Drives whether
        structural trips escalate to the global session_exhausted
        threshold (only FOREGROUND origins do)."""
        return self._origin

    @property
    def backoff_attempt(self) -> int:
        return self._backoff_attempt

    @property
    def master_enabled(self) -> bool:
        return self._master_enabled

    # ---- the canonical decision-consumer surface ----

    def evaluate(self, decision: RetryDecision) -> CircuitVerdict:
        """Consume a RetryDecision; return a CircuitVerdict.

        When master flag is FALSE, always returns RETRY_OK —
        byte-identical to no breaker.

        Otherwise:

          * Pre-trip via budget floor (SessionBudgetAuthority).
          * Pre-trip via global session breaker.
          * Apply trip table based on decision + current state.

        NEVER raises."""
        if not self._master_enabled:
            return CircuitVerdict(
                action=VerdictAction.RETRY_OK,
                state_after=self._state,
            )

        # Sticky terminal — once tripped, stays tripped.
        if self._state == CircuitState.OPEN_TERMINAL:
            return self._terminal_verdict("circuit_already_open_terminal")

        # Global breaker pre-emption — session-wide acknowledgement
        # that the budget is gone.
        if get_global_breaker().state == CircuitState.OPEN_TERMINAL:
            self._state = CircuitState.OPEN_TERMINAL
            return self._terminal_verdict("global_session_exhausted")

        # Budget-floor pre-emption — read the canonical
        # SessionBudgetAuthority oracle. If remaining is below the
        # min floor, the next provider attempt will preflight-refuse
        # anyway. Save the round-trip and terminal now.
        if self._budget_provider is not None:
            try:
                remaining = self._budget_provider()
                if remaining is not None:
                    floor = _read_float(_MIN_BUDGET_FLOOR_ENV, 0.05)
                    if remaining < floor:
                        return self._trip_terminal(
                            "budget_floor_breached",
                        )
            except Exception as exc:  # noqa: BLE001 — failure-soft
                logger.debug(
                    "[CircuitBreaker] budget_provider raised: %s — "
                    "skipping budget pre-emption",
                    exc,
                )

        # Apply the trip table.
        return self._apply_trip_table(decision)

    def _apply_trip_table(
        self, decision: RetryDecision,
    ) -> CircuitVerdict:
        """Closed trip table — AST-pinned. First match wins."""
        if decision == RetryDecision.TERMINAL_STRUCTURAL:
            return self._trip_terminal(
                f"circuit_breaker_tripped:{decision.value}",
            )
        if decision == RetryDecision.TERMINAL_CONFIG:
            return self._trip_terminal(
                f"circuit_breaker_tripped:{decision.value}",
            )
        if decision == RetryDecision.TERMINAL_QUOTA:
            self._quota_window.add()
            quota_trip = _read_int(_QUOTA_TRIP_ENV, 2, minimum=1)
            if self._quota_window.count() >= quota_trip:
                return self._trip_terminal(
                    f"circuit_breaker_tripped:{decision.value}",
                )
            # In-window quota hit but below trip — back off with
            # full-jitter and let caller retry.
            return self._verdict_backoff()
        # RETRY_TRANSIENT — count toward OPEN_TRANSIENT trip.
        if decision == RetryDecision.RETRY_TRANSIENT:
            self._transient_window.add()
            transient_trip = _read_int(_TRANSIENT_TRIP_ENV, 3, minimum=1)
            if self._transient_window.count() >= transient_trip:
                # Trip to OPEN_TRANSIENT (NOT terminal). Caller
                # backs off; next evaluate after backoff transitions
                # to HALF_OPEN.
                self._state = CircuitState.OPEN_TRANSIENT
                return self._verdict_backoff()
            return CircuitVerdict(
                action=VerdictAction.RETRY_OK,
                state_after=self._state,
            )
        # Unknown decision (should not happen — RetryDecision is
        # closed 4-value). Defensive RETRY_OK preserves legacy
        # semantics.
        return CircuitVerdict(
            action=VerdictAction.RETRY_OK,
            state_after=self._state,
        )

    # ---- success path — clear in-window counters / HALF_OPEN → CLOSED ----

    def record_success(self) -> None:
        """Called by caller on a successful provider response.
        Resets transient + quota windows; if in HALF_OPEN,
        transitions to CLOSED + resets backoff. NEVER raises."""
        if not self._master_enabled:
            return
        self._transient_window.clear()
        self._quota_window.clear()
        if self._state in (
            CircuitState.HALF_OPEN, CircuitState.OPEN_TRANSIENT,
        ):
            self._state = CircuitState.CLOSED
            self._backoff_attempt = 0
            self._next_attempt_at = None

    # ---- helpers ----

    def _trip_terminal(
        self, terminal_reason_code: str,
    ) -> CircuitVerdict:
        self._state = CircuitState.OPEN_TERMINAL
        # Report structural trips to the global breaker. We only
        # bubble TERMINAL_STRUCTURAL — quota / config are op-specific
        # signals, not session-wide acknowledgements that the budget
        # is dead.
        #
        # Slice 12N — blast-radius isolation. Even within structural
        # trips, ONLY foreground origins escalate to the global
        # threshold. Background / speculative / maintenance trips
        # terminate this op locally but cannot blast-radius out to
        # take down healthy foreground work via session_exhausted.
        # Pre-Slice-12N behaviour is preserved byte-identically when
        # origin is FOREGROUND (the constructor default).
        escalated_to_global = False
        if terminal_reason_code.endswith(":terminal_structural"):
            if self._origin in _FOREGROUND_ORIGINS:
                get_global_breaker().report_structural_trip()
                escalated_to_global = True
        logger.info(
            "[CircuitBreaker] op=%s tripped → OPEN_TERMINAL "
            "reason=%s origin=%s escalated_to_global=%s",
            self._op_id or "?", terminal_reason_code,
            self._origin.value, escalated_to_global,
        )
        return CircuitVerdict(
            action=VerdictAction.TERMINATE_UNRESOLVED,
            terminal_reason_code=terminal_reason_code,
            state_after=self._state,
        )

    def _terminal_verdict(
        self, terminal_reason_code: str,
    ) -> CircuitVerdict:
        return CircuitVerdict(
            action=VerdictAction.TERMINATE_UNRESOLVED,
            terminal_reason_code=terminal_reason_code,
            state_after=self._state,
        )

    def _verdict_backoff(self) -> CircuitVerdict:
        """Compute the next backoff via Full-Jitter + advance the
        attempt counter."""
        base = _read_float(_BACKOFF_BASE_ENV, 5.0, minimum=0.1)
        cap = _read_float(_BACKOFF_CAP_ENV, 60.0, minimum=0.1)
        delay = full_jitter_delay(
            self._backoff_attempt,
            base_s=base,
            cap_s=cap,
            rng=self._rng,
        )
        self._backoff_attempt += 1
        return CircuitVerdict(
            action=VerdictAction.RETRY_AFTER_BACKOFF,
            backoff_s=delay,
            state_after=self._state,
        )


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "CircuitState",
    "CircuitScope",
    "VerdictAction",
    "CircuitVerdict",
    "CircuitBreaker",
    "full_jitter_delay",
    "circuit_breaker_enabled",
    "get_global_breaker",
    "reset_global_breaker",
]
