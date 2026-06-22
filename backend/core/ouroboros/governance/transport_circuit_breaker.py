"""transport_circuit_breaker.py -- Sovereign Transport Circuit Breaker (Matrix A1+A2).

Three-state per-lane breaker (CLOSED / OPEN / HALF_OPEN) that rotates DW traffic
off a dead transport lane (batch) onto the healthy sibling (realtime) and
self-heals via a jittered exponential recovery timer.

Env knobs (all optional, sensible defaults):
    JARVIS_TRANSPORT_BREAKER_ENABLED   (bool, default false) -- master switch.
    JARVIS_TRANSPORT_BREAKER_FAIL_RATIO (float, default 0.5) -- trip threshold.
    JARVIS_TRANSPORT_BREAKER_MIN_SAMPLES (int, default 5)   -- min window size.
    JARVIS_TRANSPORT_BREAKER_BASE_S    (float, default 60.0) -- exp-backoff base.
    JARVIS_TRANSPORT_BREAKER_MAX_S     (float, default 600.0) -- exp-backoff cap.
    JARVIS_TRANSPORT_BREAKER_JITTER_FRAC (float, default 0.2) -- jitter band.
    JARVIS_TRANSPORT_BREAKER_WINDOW    (int, default 20)    -- rolling-window size.
    JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S (float, default 15.0) -- probe timeout.

Design constraints:
- Pure leaf module -- NO import of candidate_generator (cycle prevention).
- Fail-soft: every public method is wrapped try/except; never raises into dispatch.
- Deterministic jitter from hash(lane, consecutive_open) -- NO random module.
- Python 3.9+; asyncio.wait_for not asyncio.timeout.
- ASCII only.
"""
from __future__ import annotations

import asyncio
import collections
import enum
import hashlib
import logging
import os
from typing import Awaitable, Callable, Deque

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class BreakerState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


def breaker_enabled() -> bool:
    """Return True when the master gate env var is truthy (default false)."""
    return _env_bool("JARVIS_TRANSPORT_BREAKER_ENABLED", False)


# ---------------------------------------------------------------------------
# Per-lane state record
# ---------------------------------------------------------------------------

_SIBLINGS: dict[str, str] = {
    "batch": "realtime",
    "realtime": "batch",
}


class _LaneState:
    """Mutable record tracking breaker state for one transport lane."""

    def __init__(self, lane: str, window: int) -> None:
        self.lane: str = lane
        self.state: BreakerState = BreakerState.CLOSED
        # Rolling window of bool outcomes (True = ok, False = failure).
        self.outcomes: Deque[bool] = collections.deque(maxlen=window)
        # How many times this lane has been consecutively OPEN (drives exp backoff).
        self.consecutive_open: int = 0
        # Monotonic deadline (injected clock seconds) after which HALF_OPEN probe fires.
        self._deadline: float | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_deadline(self, now: float) -> float:
        """Compute and store the next recovery deadline (jittered exp backoff)."""
        base = _env_float("JARVIS_TRANSPORT_BREAKER_BASE_S", 60.0)
        cap = _env_float("JARVIS_TRANSPORT_BREAKER_MAX_S", 600.0)
        jitter_frac = _env_float("JARVIS_TRANSPORT_BREAKER_JITTER_FRAC", 0.2)

        wait = min(cap, base * (2.0 ** self.consecutive_open))

        # Deterministic jitter: hash(lane + consecutive_open) -> float in [-1, +1].
        seed = f"{self.lane}:{self.consecutive_open}"
        h = int(hashlib.sha256(seed.encode("ascii")).hexdigest(), 16)
        # Map 256-bit int to [0, 1], then to [-1, +1].
        unit = (h % (2 ** 32)) / (2 ** 32)  # [0, 1)
        signed = (unit * 2.0) - 1.0          # [-1, +1)
        jitter = signed * jitter_frac * wait

        self._deadline = now + wait + jitter
        return self._deadline

    def _failure_rate(self) -> float:
        """Return failure rate over the current rolling window (0.0 if empty)."""
        if not self.outcomes:
            return 0.0
        failures = sum(1 for ok in self.outcomes if not ok)
        return failures / len(self.outcomes)

    def _should_trip(self) -> bool:
        min_samples = _env_int("JARVIS_TRANSPORT_BREAKER_MIN_SAMPLES", 5)
        fail_ratio = _env_float("JARVIS_TRANSPORT_BREAKER_FAIL_RATIO", 0.5)
        return (
            len(self.outcomes) >= min_samples
            and self._failure_rate() >= fail_ratio
        )


# ---------------------------------------------------------------------------
# TransportCircuitBreaker
# ---------------------------------------------------------------------------

class TransportCircuitBreaker:
    """Process-global, per-lane three-state transport circuit breaker.

    All methods are fail-soft (try/except -> no-op/identity).
    """

    def __init__(self) -> None:
        window = _env_int("JARVIS_TRANSPORT_BREAKER_WINDOW", 20)
        self._lanes: dict[str, _LaneState] = {
            "batch": _LaneState("batch", window),
            "realtime": _LaneState("realtime", window),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_lane(self, lane: str) -> _LaneState | None:
        return self._lanes.get(lane)

    @staticmethod
    def _sibling(lane: str) -> str:
        """Return the sibling lane name; unknown lanes return themselves."""
        return _SIBLINGS.get(lane, lane)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record(
        self,
        lane: str,
        *,
        ok: bool,
        failure_mode: str | None = None,
        now: float,
    ) -> None:
        """Feed an attempt outcome into the breaker for `lane`.

        Unknown lanes are accepted (fail-soft: silently ignored for state
        tracking purposes, since we only track known lanes).
        """
        try:
            ls = self._get_lane(lane)
            if ls is None:
                # Unknown lane -- fail-soft, do nothing.
                return

            ls.outcomes.append(ok)

            if ls.state is BreakerState.CLOSED:
                if ls._should_trip():
                    _LOG.warning(
                        "[TransportBreaker] lane=%s state=CLOSED->OPEN "
                        "fail_rate=%.2f window=%d consecutive_open=%d",
                        lane,
                        ls._failure_rate(),
                        len(ls.outcomes),
                        ls.consecutive_open,
                    )
                    ls.consecutive_open += 1
                    ls._compute_deadline(now)
                    ls.state = BreakerState.OPEN
            # OPEN and HALF_OPEN states are managed via due_for_probe /
            # note_probe_result; individual record() calls don't change them.
        except Exception:
            _LOG.debug("[TransportBreaker] record() fail-soft suppressed", exc_info=True)

    def state(self, lane: str) -> BreakerState:
        """Return the current BreakerState for `lane` (CLOSED for unknowns)."""
        try:
            ls = self._get_lane(lane)
            if ls is None:
                return BreakerState.CLOSED
            return ls.state
        except Exception:
            return BreakerState.CLOSED

    def select_lane(self, preferred: str, *, now: float) -> str:
        """Return the lane to actually use.

        - CLOSED -> preferred
        - OPEN -> sibling
        - HALF_OPEN -> preferred (the probe passes through)
        - Unknown lane -> preferred (fail-soft identity)
        """
        try:
            ls = self._get_lane(preferred)
            if ls is None:
                return preferred
            if ls.state is BreakerState.OPEN:
                sib = self._sibling(preferred)
                _LOG.debug(
                    "[TransportBreaker] lane=%s OPEN -> rotating to %s", preferred, sib
                )
                return sib
            # CLOSED or HALF_OPEN: let preferred through.
            return preferred
        except Exception:
            return preferred

    def due_for_probe(self, lane: str, *, now: float) -> bool:
        """Return True when an OPEN lane's recovery deadline has elapsed.

        Side-effect on True: transitions the lane to HALF_OPEN (once).
        HALF_OPEN remains until note_probe_result() resolves it.
        """
        try:
            ls = self._get_lane(lane)
            if ls is None:
                return False
            if ls.state is not BreakerState.OPEN:
                return False
            if ls._deadline is None:
                return False
            if now >= ls._deadline:
                _LOG.warning(
                    "[TransportBreaker] lane=%s state=OPEN->HALF_OPEN probe_due now=%.1f deadline=%.1f",
                    lane,
                    now,
                    ls._deadline,
                )
                ls.state = BreakerState.HALF_OPEN
                return True
            return False
        except Exception:
            return False

    def note_probe_result(self, lane: str, *, ok: bool, now: float) -> None:
        """Resolve a HALF_OPEN probe.

        ok=True  -> CLOSED (reset counters).
        ok=False -> OPEN   (consecutive_open += 1, new longer deadline).
        """
        try:
            ls = self._get_lane(lane)
            if ls is None:
                return

            if ls.state is BreakerState.HALF_OPEN:
                if ok:
                    _LOG.warning(
                        "[TransportBreaker] lane=%s state=HALF_OPEN->CLOSED probe_ok",
                        lane,
                    )
                    ls.state = BreakerState.CLOSED
                    ls.consecutive_open = 0
                    ls._deadline = None
                    ls.outcomes.clear()
                else:
                    ls.consecutive_open += 1
                    ls._compute_deadline(now)
                    ls.state = BreakerState.OPEN
                    _LOG.warning(
                        "[TransportBreaker] lane=%s state=HALF_OPEN->OPEN "
                        "probe_fail consecutive_open=%d next_deadline=%.1f",
                        lane,
                        ls.consecutive_open,
                        ls._deadline or 0.0,
                    )
            elif ls.state is BreakerState.OPEN and ok:
                # Defensive: allow note_probe_result to close even if we missed
                # the HALF_OPEN transition (e.g. due to a reload race).
                _LOG.debug(
                    "[TransportBreaker] lane=%s note_probe_result ok=True in OPEN state (defensive close)",
                    lane,
                )
                ls.state = BreakerState.CLOSED
                ls.consecutive_open = 0
                ls._deadline = None
                ls.outcomes.clear()
        except Exception:
            _LOG.debug(
                "[TransportBreaker] note_probe_result() fail-soft suppressed", exc_info=True
            )

    def _recovery_deadline(self, lane: str) -> float:
        """Expose the current recovery deadline for testing.

        Returns 0.0 for unknown lanes or when no deadline is set.
        """
        try:
            ls = self._get_lane(lane)
            if ls is None or ls._deadline is None:
                return 0.0
            return ls._deadline
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# A2: HALF-OPEN async probe driver
# ---------------------------------------------------------------------------

async def run_probe_if_due(
    breaker: TransportCircuitBreaker,
    lane: str,
    probe_fn: Callable[[str], Awaitable[object]] | None,
    *,
    now: float,
) -> bool | None:
    """Fire a bounded async health probe when the lane is due for one.

    Protocol:
    1. Call ``breaker.due_for_probe(lane, now=now)``.
       - If False (lane not due): return None immediately.
       - If True: lane transitions OPEN -> HALF_OPEN inside due_for_probe.
    2. ``await asyncio.wait_for(probe_fn(lane), timeout=<env>)``
    3. Call ``breaker.note_probe_result(lane, ok=<truthiness>, now=now)``.
    4. Return the raw probe result.

    Fail-soft: a probe that raises OR times out counts as ok=False.
    Never propagates exceptions into the caller.

    Env:
        JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S (float, default 15.0)
    """
    if not breaker.due_for_probe(lane, now=now):
        return None

    timeout = _env_float("JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S", 15.0)
    _SENTINEL = object()  # marks the error path
    result: object = _SENTINEL
    ok = False
    try:
        result = await asyncio.wait_for(probe_fn(lane), timeout=timeout)  # type: ignore[misc]
        ok = bool(result)
    except Exception:
        _LOG.debug(
            "[TransportBreaker] probe lane=%s timed-out or raised; ok=False",
            lane,
            exc_info=True,
        )
        ok = False

    breaker.note_probe_result(lane, ok=ok, now=now)

    # Error path (timeout or raise): sentinel still set -> return False.
    # Normal path: return the actual probe result (truthy or falsy).
    if result is _SENTINEL:
        return False
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_SINGLETON: TransportCircuitBreaker | None = None


def get_transport_breaker() -> TransportCircuitBreaker:
    """Return the process-global TransportCircuitBreaker singleton."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = TransportCircuitBreaker()
    return _SINGLETON
