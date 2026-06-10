"""Slice 206 — Boot-Warmup Lifecycle (honest starvation reclassification).

The 59 control-plane-starvation events were MISLEADING — they conflated
one-time boot-warmup blocking (heavy semantic / posture / oracle init) with
genuine steady-state starvation. This module formalizes a BOOT_WARMUP →
STEADY_STATE lifecycle so the watchdog can record warmup-window lag as a
DISTINCT, VISIBLE ``warmup_lag`` counter (not hidden) while
``control_plane_starvation_events`` only counts POST-warmup events — making
that metric mean what it claims.

ANTI-GAMING GUARD: a HARD warmup deadline (``JARVIS_INIT_WARMUP_MAX_S``,
default 180s) force-transitions to STEADY_STATE regardless of any completion
signal. "Warmup" can never be claimed indefinitely to mask real starvation.

Gated ``JARVIS_INIT_LIFECYCLE_ENABLED`` default-FALSE — OFF reports
STEADY_STATE always (byte-identical pre-206 behavior). All functions take an
injectable ``now`` for testability and NEVER raise.
"""
from __future__ import annotations

import enum
import os
import threading
import time
from typing import Optional

_ENV_ENABLED = "JARVIS_INIT_LIFECYCLE_ENABLED"
_ENV_MAX_S = "JARVIS_INIT_WARMUP_MAX_S"
_DEFAULT_MAX_S = 180.0


class LifecyclePhase(str, enum.Enum):
    BOOT_WARMUP = "boot_warmup"
    STEADY_STATE = "steady_state"


_lock = threading.Lock()
_warmup_start: Optional[float] = None
_warmup_done: bool = False


def init_lifecycle_enabled() -> bool:
    """Gate, default FALSE. NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _max_warmup_s() -> float:
    try:
        raw = os.environ.get(_ENV_MAX_S, "").strip()
        v = float(raw) if raw else _DEFAULT_MAX_S
        return v if v > 0 else _DEFAULT_MAX_S
    except Exception:  # noqa: BLE001
        return _DEFAULT_MAX_S


def _now(now: Optional[float]) -> float:
    if now is not None:
        return float(now)
    try:
        return time.time()
    except Exception:  # noqa: BLE001
        return 0.0


def start_warmup(now: Optional[float] = None) -> None:
    """Enter BOOT_WARMUP at boot. NEVER raises."""
    global _warmup_start, _warmup_done
    with _lock:
        _warmup_start = _now(now)
        _warmup_done = False


def mark_warmup_complete(now: Optional[float] = None) -> None:
    """Signal warmup finished (proactive warmup tasks done). NEVER raises."""
    global _warmup_done
    with _lock:
        _warmup_done = True


def in_warmup(now: Optional[float] = None) -> bool:
    """True iff the lifecycle is ON, warmup was started, not explicitly
    completed, AND the hard deadline has not elapsed. NEVER raises."""
    try:
        if not init_lifecycle_enabled():
            return False
        with _lock:
            if _warmup_start is None or _warmup_done:
                return False
            start = _warmup_start
        return (_now(now) - start) < _max_warmup_s()
    except Exception:  # noqa: BLE001
        return False


def current_phase(now: Optional[float] = None) -> LifecyclePhase:
    return LifecyclePhase.BOOT_WARMUP if in_warmup(now) \
        else LifecyclePhase.STEADY_STATE


def reset_for_tests() -> None:
    global _warmup_start, _warmup_done
    with _lock:
        _warmup_start = None
        _warmup_done = False
