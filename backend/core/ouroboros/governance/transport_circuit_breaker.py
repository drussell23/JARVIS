"""transport_circuit_breaker.py -- Sovereign Transport Circuit Breaker (Matrix A).

Three-state per-lane breaker (CLOSED / OPEN / HALF_OPEN) that rotates DW traffic
off a dead transport lane (batch) onto the healthy sibling (realtime) and
self-heals via an async HALF-OPEN probe.

REUSE-FIRST (no duplicate health/timer logic):
- Recovery-window timing is delegated to ``dw_transport_recovery.DWTransportRecovery``
  (one instance PER lane) -- the Slice-127-P3 dynamic full-jitter exponential window
  + Slice-242 adaptive prior, which itself composes ``circuit_breaker.full_jitter_delay``.
  This module does NOT reimplement jitter or exponential backoff.
- The genuinely-new layer is the per-lane state machine + ``select_lane`` ROTATION
  (batch OPEN -> realtime) + the async probe. None of the existing modules
  (``dual_lane_breaker`` = total-outage terminal pause; ``dw_transport_recovery`` =
  recovery-window timing) rotates traffic between lanes; that is this module's job.
- Composition with ``dual_lane_breaker``: a SINGLE dead lane -> we rotate; BOTH lanes
  dead -> the sibling is also OPEN so ``select_lane`` stops rotating and returns the
  preferred lane, leaving the total-outage terminal pause to ``dual_lane_breaker``.

Env knobs owned HERE (trip + window + probe -- the new layer):
    JARVIS_TRANSPORT_BREAKER_ENABLED    (bool, default false) -- master switch.
    JARVIS_TRANSPORT_BREAKER_FAIL_RATIO (float, default 0.5) -- trip failure-rate.
    JARVIS_TRANSPORT_BREAKER_MIN_SAMPLES (int, default 5)   -- min rolling samples.
    JARVIS_TRANSPORT_BREAKER_WINDOW     (int, default 20)   -- rolling-window size.
    JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S (float, default 15.0) -- probe timeout.
Recovery-window knobs are the EXISTING ``dw_transport_recovery`` ones
(``JARVIS_DW_RECOVERY_BASE_S`` default 30, ``JARVIS_DW_RECOVERY_CAP_S`` default 600) --
NOT redefined here.

Design constraints:
- Pure leaf wrt the dispatch loop -- NO import of candidate_generator (cycle prevention).
- Fail-soft: every public method is wrapped try/except; never raises into dispatch.
- Recovery jitter is the EXISTING full-jitter primitive (deterministic when a seeded
  ``rng`` is injected for tests; real jitter in production).
- Python 3.9+; asyncio.wait_for not asyncio.timeout. ASCII only.
"""
from __future__ import annotations

import asyncio
import collections
import enum
import logging
import os
from typing import Any, Awaitable, Callable, Deque, Optional

from backend.core.ouroboros.governance.dw_transport_recovery import (
    DWTransportRecovery,
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
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


_SIBLINGS: dict[str, str] = {"batch": "realtime", "realtime": "batch"}


# ---------------------------------------------------------------------------
# Per-lane state record
# ---------------------------------------------------------------------------

class _LaneState:
    """Breaker state for one transport lane.

    The recovery-window timing is OWNED by an embedded ``DWTransportRecovery``
    (reused, not reimplemented); this record only adds the state machine + the
    adaptive failure-rate trip decision.
    """

    def __init__(self, lane: str, window: int, rng: Optional[Any]) -> None:
        self.lane: str = lane
        self.state: BreakerState = BreakerState.CLOSED
        # Rolling window of bool outcomes (True = ok, False = failure).
        self.outcomes: Deque[bool] = collections.deque(maxlen=window)
        # REUSE: episode tracker + dynamic full-jitter recovery window + prior.
        self.recovery: DWTransportRecovery = DWTransportRecovery()
        # Injected RNG for deterministic recovery-window jitter in tests.
        self._rng: Optional[Any] = rng
        # Monotonic deadline (injected-clock seconds) after which the probe fires.
        self._deadline: Optional[float] = None

    # -- recovery timing (delegated) -----------------------------------

    def _arm_recovery(self, now: float) -> None:
        """Register a degraded episode and arm the next probe deadline using the
        EXISTING dynamic recovery window (no local backoff math)."""
        self.recovery.note_degraded(now=now)
        window = self.recovery.dynamic_recovery_window_s(rng=self._rng)
        self._deadline = now + float(window)

    def _clear_recovery(self) -> None:
        """A probe succeeded: reset episodes instantly (existing semantics)."""
        self.recovery.note_recovered()
        self._deadline = None
        self.outcomes.clear()

    # -- trip decision (the new adaptive layer) ------------------------

    def _failure_rate(self) -> float:
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

    All methods are fail-soft (try/except -> no-op/identity). Pass a seeded
    ``rng`` (e.g. ``random.Random(0)``) for deterministic recovery-window jitter
    in tests; production leaves it ``None`` (real full jitter).
    """

    def __init__(self, rng: Optional[Any] = None) -> None:
        window = _env_int("JARVIS_TRANSPORT_BREAKER_WINDOW", 20)
        self._lanes: dict[str, _LaneState] = {
            "batch": _LaneState("batch", window, rng),
            "realtime": _LaneState("realtime", window, rng),
        }

    def _get_lane(self, lane: str) -> Optional[_LaneState]:
        return self._lanes.get(lane)

    def _sibling_state(self, lane: str) -> BreakerState:
        sib = self._lanes.get(_SIBLINGS.get(lane, lane))
        return sib.state if sib is not None else BreakerState.CLOSED

    @staticmethod
    def _sibling(lane: str) -> str:
        return _SIBLINGS.get(lane, lane)

    # -- public interface ----------------------------------------------

    def record(
        self,
        lane: str,
        *,
        ok: bool,
        failure_mode: Optional[str] = None,
        now: float,
    ) -> None:
        """Feed an attempt outcome for ``lane`` (unknown lanes ignored)."""
        try:
            ls = self._get_lane(lane)
            if ls is None:
                return
            ls.outcomes.append(ok)
            if ls.state is BreakerState.CLOSED and ls._should_trip():
                _LOG.warning(
                    "[TransportBreaker] lane=%s CLOSED->OPEN fail_rate=%.2f "
                    "window=%d mode=%s episodes=%d",
                    lane, ls._failure_rate(), len(ls.outcomes),
                    failure_mode or "-", ls.recovery.episode_count,
                )
                ls._arm_recovery(now)
                ls.state = BreakerState.OPEN
        except Exception:  # noqa: BLE001 — never raise into dispatch
            _LOG.debug("[TransportBreaker] record() fail-soft", exc_info=True)

    def state(self, lane: str) -> BreakerState:
        try:
            ls = self._get_lane(lane)
            return ls.state if ls is not None else BreakerState.CLOSED
        except Exception:  # noqa: BLE001
            return BreakerState.CLOSED

    def select_lane(self, preferred: str, *, now: float) -> str:
        """Return the lane to actually use.

        - CLOSED / HALF_OPEN -> preferred (the probe passes through HALF_OPEN).
        - OPEN, sibling usable -> rotate to sibling.
        - OPEN, sibling ALSO OPEN (total outage) -> return preferred and let
          ``dual_lane_breaker`` own the terminal pause (we don't rotate onto a
          second dead lane).
        - Unknown lane -> preferred (fail-soft identity).
        """
        try:
            ls = self._get_lane(preferred)
            if ls is None:
                return preferred
            if ls.state is BreakerState.OPEN:
                if self._sibling_state(preferred) is BreakerState.OPEN:
                    _LOG.debug(
                        "[TransportBreaker] both lanes OPEN — no rotate "
                        "(dual_lane_breaker owns total-outage); lane=%s",
                        preferred,
                    )
                    return preferred
                sib = self._sibling(preferred)
                _LOG.debug(
                    "[TransportBreaker] lane=%s OPEN -> rotating to %s",
                    preferred, sib,
                )
                return sib
            return preferred
        except Exception:  # noqa: BLE001
            return preferred

    def due_for_probe(self, lane: str, *, now: float) -> bool:
        """True when an OPEN lane's recovery deadline elapsed; transitions to
        HALF_OPEN (once) as a side effect."""
        try:
            ls = self._get_lane(lane)
            if ls is None or ls.state is not BreakerState.OPEN:
                return False
            if ls._deadline is None or now < ls._deadline:
                return False
            _LOG.warning(
                "[TransportBreaker] lane=%s OPEN->HALF_OPEN probe_due "
                "now=%.1f deadline=%.1f", lane, now, ls._deadline,
            )
            ls.state = BreakerState.HALF_OPEN
            return True
        except Exception:  # noqa: BLE001
            return False

    def note_probe_result(self, lane: str, *, ok: bool, now: float) -> None:
        """Resolve a probe: ok -> CLOSED (reset episodes); fail -> OPEN (next,
        longer window)."""
        try:
            ls = self._get_lane(lane)
            if ls is None:
                return
            if ls.state is BreakerState.HALF_OPEN:
                if ok:
                    _LOG.warning(
                        "[TransportBreaker] lane=%s HALF_OPEN->CLOSED probe_ok",
                        lane,
                    )
                    ls.state = BreakerState.CLOSED
                    ls._clear_recovery()
                else:
                    ls._arm_recovery(now)
                    ls.state = BreakerState.OPEN
                    _LOG.warning(
                        "[TransportBreaker] lane=%s HALF_OPEN->OPEN probe_fail "
                        "episodes=%d next_deadline=%.1f",
                        lane, ls.recovery.episode_count, ls._deadline or 0.0,
                    )
            elif ls.state is BreakerState.OPEN and ok:
                # Defensive: close even if the HALF_OPEN transition was missed
                # (reload race). Conservative — never dangerous.
                _LOG.debug(
                    "[TransportBreaker] lane=%s probe_ok in OPEN (defensive close)",
                    lane,
                )
                ls.state = BreakerState.CLOSED
                ls._clear_recovery()
        except Exception:  # noqa: BLE001
            _LOG.debug("[TransportBreaker] note_probe_result() fail-soft", exc_info=True)

    def _recovery_deadline(self, lane: str) -> float:
        """Expose the current recovery deadline (testing). 0.0 if unset."""
        try:
            ls = self._get_lane(lane)
            return ls._deadline if (ls and ls._deadline is not None) else 0.0
        except Exception:  # noqa: BLE001
            return 0.0


# ---------------------------------------------------------------------------
# A2: HALF-OPEN async probe driver
# ---------------------------------------------------------------------------

async def run_probe_if_due(
    breaker: TransportCircuitBreaker,
    lane: str,
    probe_fn: Optional[Callable[[str], Awaitable[object]]],
    *,
    now: float,
) -> Optional[bool]:
    """Fire a bounded async health probe when the lane is due.

    Calls ``due_for_probe`` FIRST (OPEN->HALF_OPEN), then
    ``asyncio.wait_for(probe_fn(lane), timeout=<env>)``, then
    ``note_probe_result``. A probe that raises OR times out counts as ok=False.
    Returns None when not due. Never propagates exceptions.

    Env: JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S (float, default 15.0)
    """
    if not breaker.due_for_probe(lane, now=now):
        return None
    timeout = _env_float("JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S", 15.0)
    _SENTINEL = object()
    result: object = _SENTINEL
    ok = False
    try:
        result = await asyncio.wait_for(probe_fn(lane), timeout=timeout)  # type: ignore[misc]
        ok = bool(result)
    except Exception:  # noqa: BLE001 — timeout/raise => ok=False
        _LOG.debug("[TransportBreaker] probe lane=%s timed-out/raised", lane, exc_info=True)
        ok = False
    breaker.note_probe_result(lane, ok=ok, now=now)
    if result is _SENTINEL:
        return False
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_SINGLETON: Optional[TransportCircuitBreaker] = None


def get_transport_breaker() -> TransportCircuitBreaker:
    """Return the process-global TransportCircuitBreaker singleton."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = TransportCircuitBreaker()
    return _SINGLETON
