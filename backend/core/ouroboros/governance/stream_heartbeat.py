"""Streaming liveness heartbeat -- feeds the IDLE/staleness watchdog (NOT the
wall-clock cap).

The Inter-Token Watchdog pulses this on every streamed delta so a *streaming*
generation phase stays "fresh" for the idle/staleness watchdog (the Move-2-v4
``last_activity_at_utc`` path, which is designed to yield to activity). A streaming
op is therefore never idle-killed while tokens flow.

DELIBERATELY NOT consumed by the wall-clock hard cap: per the Slice-47 / Phase-D
Watchdog Isolation Invariant, the wall-clock cap MUST remain blind to application
activity (a watchdog that yields to a signal the system it guards can emit is not a
watchdog -- the 11h/22-min-late runaway class). The inter-token watchdog is the
per-call progress guard; the checkpoint/resume hydrator is how a thought survives
the blind wall. This heartbeat only informs the *idle* path.

In-process module global (fast, lock-guarded) + an optional cross-process file
mirror (env ``JARVIS_STREAM_HEARTBEAT_FILE``) for a driver in another process.
"""
from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_last_pulse_monotonic: float = 0.0
_pulse_count: int = 0


def pulse() -> None:
    """Record a streamed-token liveness beat. Called per delta by the Inter-Token
    Watchdog. Best-effort cross-process mirror; NEVER raises."""
    global _last_pulse_monotonic, _pulse_count
    with _lock:
        _last_pulse_monotonic = time.monotonic()
        _pulse_count += 1
    path = os.environ.get("JARVIS_STREAM_HEARTBEAT_FILE")
    if path:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(time.time()))
        except Exception:  # noqa: BLE001 -- mirror is best-effort
            pass


def seconds_since_pulse() -> float:
    """Wall-seconds since the last beat; ``inf`` if never pulsed. NEVER raises."""
    with _lock:
        if _last_pulse_monotonic <= 0.0:
            return float("inf")
        return max(0.0, time.monotonic() - _last_pulse_monotonic)


def is_active(window_s: float) -> bool:
    """True iff a beat landed within *window_s* (the stream is actively emitting)."""
    return seconds_since_pulse() <= max(0.0, window_s)


def pulse_count() -> int:
    with _lock:
        return _pulse_count


def reset() -> None:
    """Test hook -- clear the heartbeat state."""
    global _last_pulse_monotonic, _pulse_count
    with _lock:
        _last_pulse_monotonic = 0.0
        _pulse_count = 0
