"""Control-plane load-shed signal (CD-2). A process-level latch set while an LLM
stream is active AND the event loop is critically lagged, so the SensorGovernor
sheds low-priority background work to free the loop for stream consumption.
Reuses control_plane_watchdog (lag) + SensorGovernor (brake) — no new governor.
Gated by JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED (default OFF)."""
from __future__ import annotations

import os
import threading

_TRUE = {"1", "true", "yes", "on"}
_lock = threading.Lock()
_stream_active = 0          # reentrancy count (nested/concurrent streams)
_shed_active = False


def load_shed_enabled() -> bool:
    return os.environ.get("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "").strip().lower() in _TRUE


def critical_lag_threshold_ms() -> float:
    return float(os.environ.get("JARVIS_LOAD_SHED_LAG_THRESHOLD_MS", "150"))


def stream_begin() -> None:
    global _stream_active
    with _lock:
        _stream_active += 1


def stream_end() -> None:
    global _stream_active, _shed_active
    with _lock:
        _stream_active = max(0, _stream_active - 1)
        if _stream_active == 0:
            _shed_active = False   # restore when no stream is active


def evaluate(recent_lag_ms: float) -> bool:
    """Update + return the shed latch: shed iff enabled AND a stream is active AND
    recent lag exceeds the critical threshold. Latches ON during the stream; clears
    on stream_end. Returns current shed state."""
    global _shed_active
    if not load_shed_enabled():
        return False
    with _lock:
        if _stream_active > 0 and float(recent_lag_ms) >= critical_lag_threshold_ms():
            _shed_active = True
        return _shed_active


def is_shedding() -> bool:
    with _lock:
        return _shed_active and load_shed_enabled()


def _reset_for_test() -> None:
    global _stream_active, _shed_active
    with _lock:
        _stream_active = 0
        _shed_active = False
