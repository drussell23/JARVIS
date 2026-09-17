"""Control-plane load-shed signal (CD-2). A process-level latch set while an LLM
stream is active AND the event loop is critically lagged, so the SensorGovernor
sheds low-priority background work to free the loop for stream consumption.
Reuses control_plane_watchdog (lag) + SensorGovernor (brake) — no new governor.
Gated by JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED (default OFF).

## Telemetry backpressure (second consumer)

Measured in bt-2026-09-09-024244, while an 8-agent exploration fleet ran::

    [ControlPlaneStarvation] lag_ms=1739.6 (requested=100.0 observed=1839.6)
        threshold=500.0 event_n=34 — main asyncio loop is starved

11x the warn threshold, and nothing shed: this latch requires a STREAM to be
active, and none was. That precondition is correct for its first consumer —
sensor work is shed to protect stream CONSUMPTION, so with no stream there is
nothing to protect. It is wrong for telemetry, where the starvation itself is
the whole reason to back off. :func:`telemetry_shedding` therefore asks the
same lag source against the same enable flag, and drops the stream
precondition, with the reason recorded here rather than rediscovered later.

What may be shed is narrow BY CONSTRUCTION: the observability COPY of an event
(the spine bridge — already `persist=False`, already documented non-fatal),
never delivery to a subscriber. A subscriber is a control path; an event it
misses is a decision that does not happen. Backpressure that can drop those is
not backpressure, it is data loss with a nicer name.

Shedding is accounted, never silent — :func:`shed_counts` names every topic
whose telemetry was dropped, so the gap in the record is itself in the record.
"""
from __future__ import annotations

import os
import threading
from typing import Dict, Optional

_TRUE = {"1", "true", "yes", "on"}
_lock = threading.Lock()
_stream_active = 0          # reentrancy count (nested/concurrent streams)
_shed_active = False
_shed_counts: Dict[str, int] = {}


def load_shed_enabled() -> bool:
    return os.environ.get("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "").strip().lower() in _TRUE


def critical_lag_threshold_ms() -> float:
    return float(os.environ.get("JARVIS_LOAD_SHED_LAG_THRESHOLD_MS", "150"))


def telemetry_threshold_ms() -> float:
    """Lag at which the observability copy of an event stops being worth its
    place on the loop.

    Derived from the control-plane watchdog's OWN warn threshold: that number
    is already this system's definition of "the loop is starved", and a second
    independent constant would let the two drift until the logs say starved
    while the shedder says fine. Overridable, and falling back to the latch's
    existing critical threshold if the watchdog cannot be read.
    """
    raw = os.environ.get("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    try:
        from backend.core.ouroboros.governance.control_plane_watchdog import (  # noqa: PLC0415
            _resolve_threshold_ms,
        )
        v = float(_resolve_threshold_ms())
        if v > 0:
            return v
    except Exception:  # noqa: BLE001
        pass
    return critical_lag_threshold_ms()


def telemetry_shedding(lag_ms: Optional[float] = None) -> bool:
    """Whether non-critical telemetry should be dropped right now.

    Unlike :func:`evaluate` this does NOT latch and does NOT require a stream:
    it is a reading of the loop's current state, so it lifts by itself the
    moment the loop recovers, with no stream boundary to clear it.

    Unknown lag never sheds. The watchdog returns ``0.0`` both for "healthy"
    and for "could not be read", and those must resolve the same way here —
    toward KEEPING the data. Dropping observability on an unreadable signal is
    how a system goes blind exactly when something is wrong with it.
    """
    if not load_shed_enabled():
        return False
    try:
        if lag_ms is None:
            from backend.core.ouroboros.governance.control_plane_watchdog import (  # noqa: PLC0415
                recent_lag_ms,
            )
            lag_ms = recent_lag_ms()
        lag = float(lag_ms or 0.0)
    except Exception:  # noqa: BLE001
        return False
    if lag <= 0.0:
        return False
    return lag >= telemetry_threshold_ms()


def note_shed(topic: str) -> None:
    """Record that *topic*'s telemetry was dropped. NEVER raises."""
    try:
        key = str(topic or "?")[:120]
        with _lock:
            _shed_counts[key] = _shed_counts.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def shed_counts() -> Dict[str, int]:
    """Per-topic tally of dropped telemetry — the gap, on the record."""
    with _lock:
        return dict(_shed_counts)


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
        _shed_counts.clear()
