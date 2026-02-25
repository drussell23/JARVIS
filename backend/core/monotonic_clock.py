"""
Monotonic Clock Utilities v1.0 — Phase 11 Hardening

Provides monotonic timing primitives for lifecycle-critical timeout paths.

Problem:
    time.time() fails under clock jumps, NTP adjustments, and suspend/resume.
    These failures cause false-positive kills, double-ownership of locks, and
    corrupted golden images when lifecycle-critical deadlines use wall-clock.

Solution:
    MonotonicDeadline   — replacement for `start = time.time(); if time.time() - start > timeout`
    MonotonicStopwatch  — lightweight elapsed-time tracker (no deadline)
    drift_detected()    — detects wall-clock vs monotonic divergence (diagnostic only)
    monotonic_now()     — fail-open wrapper for time.monotonic()

Constraints:
    - Fail-open: if monotonic_clock itself errors, callers get raw time.time() behavior
    - Zero allocation in hot path (deadline check is a float comparison)
    - Thread-safe, async-compatible
    - All convenience functions catch internal errors and fall through

Copyright (c) 2026 JARVIS AI. All rights reserved.
"""

import logging
import os
import time
from typing import Union  # noqa: F401

logger = logging.getLogger("jarvis.monotonic_clock")

# ---------------------------------------------------------------------------
# Module-level convenience: fail-open monotonic
# ---------------------------------------------------------------------------

def monotonic_now() -> float:
    """Return time.monotonic(). Fail-open: returns time.time() on any error."""
    try:
        return time.monotonic()
    except Exception:
        return time.time()


# ---------------------------------------------------------------------------
# MonotonicDeadline
# ---------------------------------------------------------------------------

class MonotonicDeadline:
    """
    A deadline tracker immune to wall-clock jumps.

    Replaces the pattern::

        start = time.time()
        while time.time() - start < timeout:
            ...

    With::

        deadline = MonotonicDeadline(timeout, label="my_op")
        while not deadline.is_expired():
            ...

    Stores both monotonic and wall-clock start times.  The monotonic value
    is used for ALL deadline/elapsed calculations.  The wall-clock value is
    stored ONLY for backward-compatible logging (log parsers, dashboards).

    Thread-safe: all reads are of immutable floats or via simple float
    arithmetic — no locking required.
    """

    __slots__ = ("_start_mono", "_timeout", "_start_wall", "_label")

    def __init__(self, timeout: float, label: str = ""):
        self._start_mono: float = time.monotonic()
        self._start_wall: float = time.time()
        self._timeout: float = float(timeout)
        self._label: str = label

    # -- Core queries --------------------------------------------------------

    def elapsed(self) -> float:
        """Monotonic seconds since creation (or last reset)."""
        return time.monotonic() - self._start_mono

    def remaining(self) -> float:
        """Seconds remaining until expiry (never negative)."""
        return max(0.0, self._timeout - self.elapsed())

    def is_expired(self) -> bool:
        """True when the deadline has been reached."""
        return self.elapsed() >= self._timeout

    @property
    def timeout(self) -> float:
        """The current deadline duration (may change via extend)."""
        return self._timeout

    # -- Mutations -----------------------------------------------------------

    def extend(self, additional_seconds: float) -> None:
        """
        Extend the deadline by *additional_seconds*.

        Used by ProgressController when pressure or progress warrants grace.
        """
        self._timeout += additional_seconds

    def reset(self) -> None:
        """
        Restart the clock from now.

        Used by DMS after a successful component restart.
        """
        self._start_mono = time.monotonic()
        self._start_wall = time.time()

    # -- Backward-compat accessors -------------------------------------------

    def wall_start(self) -> float:
        """Return the wall-clock start time (for logging only)."""
        return self._start_wall

    def __repr__(self) -> str:
        return (
            f"MonotonicDeadline(label={self._label!r}, "
            f"timeout={self._timeout:.1f}s, "
            f"elapsed={self.elapsed():.1f}s, "
            f"expired={self.is_expired()})"
        )


# ---------------------------------------------------------------------------
# MonotonicStopwatch
# ---------------------------------------------------------------------------

class MonotonicStopwatch:
    """
    Lightweight elapsed-time tracker (no deadline).

    Useful for measuring durations in loops without a timeout.
    """

    __slots__ = ("_start_mono", "_last_lap_mono")

    def __init__(self) -> None:
        now = time.monotonic()
        self._start_mono: float = now
        self._last_lap_mono: float = now

    def elapsed(self) -> float:
        """Seconds since creation (or last reset)."""
        return time.monotonic() - self._start_mono

    def lap(self) -> float:
        """Seconds since last lap (or creation). Resets the lap marker."""
        now = time.monotonic()
        delta = now - self._last_lap_mono
        self._last_lap_mono = now
        return delta

    def reset(self) -> None:
        """Restart the stopwatch from now."""
        now = time.monotonic()
        self._start_mono = now
        self._last_lap_mono = now

    def __repr__(self) -> str:
        return f"MonotonicStopwatch(elapsed={self.elapsed():.3f}s)"


# ---------------------------------------------------------------------------
# Drift detection (diagnostic only)
# ---------------------------------------------------------------------------

_DRIFT_THRESHOLD_DEFAULT = float(
    os.environ.get("JARVIS_CLOCK_DRIFT_THRESHOLD", "2.0")
)


def drift_detected(
    wall_start: float,
    mono_start: float,
    threshold: float = _DRIFT_THRESHOLD_DEFAULT,
) -> bool:
    """
    Check if wall clock has drifted >threshold seconds from monotonic.

    Uses the invariant that ``(wall_now - wall_start)`` and
    ``(mono_now - mono_start)`` should be nearly equal.  If their difference
    exceeds *threshold*, a clock jump (NTP/suspend) has occurred.

    This is a *diagnostic* function — never used for gating decisions.

    Fail-open: returns False on any internal error.
    """
    try:
        wall_elapsed = time.time() - wall_start
        mono_elapsed = time.monotonic() - mono_start
        return abs(wall_elapsed - mono_elapsed) > threshold
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fail-open convenience factory
# ---------------------------------------------------------------------------

def monotonic_deadline(timeout: float, label: str = "") -> Union[MonotonicDeadline, "_WallClockFallbackDeadline"]:
    """
    Create a MonotonicDeadline.

    Fail-open: if construction somehow fails (should never happen),
    returns a wall-clock-based fallback that quacks like MonotonicDeadline.
    """
    try:
        return MonotonicDeadline(timeout, label=label)
    except Exception as exc:
        logger.warning(
            "[monotonic_clock] Failed to create MonotonicDeadline, "
            "using wall-clock fallback: %s",
            exc,
        )
        return _WallClockFallbackDeadline(timeout, label)


class _WallClockFallbackDeadline:
    """
    Emergency fallback if MonotonicDeadline construction fails.
    Uses time.time() — the very thing we're trying to avoid — but at
    least callers don't crash.
    """

    __slots__ = ("_start", "_timeout", "_label")

    def __init__(self, timeout: float, label: str = ""):
        self._start = time.time()
        self._timeout = float(timeout)
        self._label = label

    def elapsed(self) -> float:
        return time.time() - self._start

    def remaining(self) -> float:
        return max(0.0, self._timeout - self.elapsed())

    def is_expired(self) -> bool:
        return self.elapsed() >= self._timeout

    @property
    def timeout(self) -> float:
        return self._timeout

    def extend(self, additional_seconds: float) -> None:
        self._timeout += additional_seconds

    def reset(self) -> None:
        self._start = time.time()

    def wall_start(self) -> float:
        return self._start
