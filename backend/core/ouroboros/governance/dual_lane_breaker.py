"""Slice 53 — dual-lane total-outage circuit breaker.

Closes the DW-only blackout gap surfaced by v45/v46 + the Slice 51 vendor
repro: when DoubleWord returns empty token streams under HTTP 200 on BOTH the
streaming preflight AND the batch generation, every GENERATE op exhausts all DW
models and burns retry tokens indefinitely. The existing
``ProviderExhaustionWatcher`` does not catch this because DW-only exhaustions
raise ``fallback_skipped:no_fallback_configured`` which is deliberately NOT
counted toward its hibernation threshold (so single-fallback-less ops don't
hibernate the loop).

Option A — gated dual-lane isolation. A single GENERATE op only fails to yield
a candidate from EITHER lane when BOTH lanes are empty (streaming degraded AND
batch generation empty). So:

  * ``record_total_outage()`` — call when an op exhausts all DW models with no
    candidate from streaming or batch. Increments a consecutive counter.
  * ``record_success()`` — call when any lane yields a candidate. Resets the
    counter. This is what preserves Slice 41's ACTIVE_BATCH_ONLY posture:
    single-lane streaming degradation still produces a batch candidate, so the
    counter never climbs to the threshold.
  * Trips when ``JARVIS_TOTAL_OUTAGE_THRESHOLD`` (default 3) CONSECUTIVE total
    outages accumulate — a verified total vendor blackout, not a transient.

A tripped breaker is terminal for the session: the orchestrator requests a
graceful, exit-code-0 pause (state already serialized to summary.json) rather
than exhausting retry tokens. A late candidate does not silently un-trip it.

Pure, thread-safe, env-driven. NEVER raises.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

_DEFAULT_THRESHOLD = 3
_ENABLED_ENV = "JARVIS_DUAL_LANE_BREAKER_ENABLED"
_THRESHOLD_ENV = "JARVIS_TOTAL_OUTAGE_THRESHOLD"


def _enabled() -> bool:
    """Master switch (default ON). ``=0/false/no/off`` disables — the breaker
    then records nothing and never trips (byte-identical to pre-Slice-53)."""
    return (
        os.environ.get(_ENABLED_ENV, "true").strip().lower()
        not in ("0", "false", "no", "off")
    )


def _threshold() -> int:
    """Consecutive total-outage count required to trip (default 3, floor 1)."""
    try:
        v = int(os.environ.get(_THRESHOLD_ENV, "").strip())
        return max(1, v)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


@dataclass(frozen=True)
class BreakerState:
    """Immutable snapshot for observability surfaces."""

    consecutive_total_outages: int
    tripped: bool
    last_diagnostic: str
    threshold: int
    enabled: bool


class DualLaneOutageBreaker:
    """Thread-safe consecutive-total-outage counter with a terminal trip."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive = 0
        self._tripped = False
        self._last_diag = ""

    def record_total_outage(self, diagnostic: str = "") -> bool:
        """Record one op that exhausted BOTH lanes with no candidate.

        Returns ``True`` exactly once — on the call that trips the breaker —
        so the caller can fire the graceful-pause request a single time.
        Returns ``False`` while disabled, below threshold, or already tripped.
        """
        if not _enabled():
            return False
        with self._lock:
            if self._tripped:
                return False
            self._consecutive += 1
            if diagnostic:
                self._last_diag = diagnostic[:200]
            if self._consecutive >= _threshold():
                self._tripped = True
                return True
            return False

    def record_success(self) -> None:
        """Record that some lane yielded a candidate — resets the consecutive
        counter (preserves Slice 41 single-lane resilience). Does NOT un-trip a
        breaker that has already verified a total blackout."""
        with self._lock:
            self._consecutive = 0

    def is_tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def reset(self) -> None:
        """Full reset (new session / tests)."""
        with self._lock:
            self._consecutive = 0
            self._tripped = False
            self._last_diag = ""

    def snapshot(self) -> BreakerState:
        with self._lock:
            return BreakerState(
                consecutive_total_outages=self._consecutive,
                tripped=self._tripped,
                last_diagnostic=self._last_diag,
                threshold=_threshold(),
                enabled=_enabled(),
            )


# Process-wide singleton — candidate_generator records outcomes, the loop
# service polls is_tripped(). Lazy so import is side-effect-free.
_DEFAULT_BREAKER: "DualLaneOutageBreaker | None" = None
_DEFAULT_LOCK = threading.Lock()


def get_dual_lane_breaker() -> DualLaneOutageBreaker:
    global _DEFAULT_BREAKER
    if _DEFAULT_BREAKER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_BREAKER is None:
                _DEFAULT_BREAKER = DualLaneOutageBreaker()
    return _DEFAULT_BREAKER
