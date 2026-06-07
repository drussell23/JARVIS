"""Slice 127 Phase 3 — dynamic full-jitter exponential DW recovery window.

P3's *core* DW self-healing already exists on main: a severed DW lane
(``DIRECT_STREAMING → TRANSPORT_DEGRADED``) auto-probes back once the verdict
goes stale, gated by a **hardcoded-default 120s** freshness window
(``candidate_generator._dw_preflight_freshness_s``). That static timer is the
one thing to improve: a fixed pause re-probes a chronically-rupturing lane on a
rigid cadence (thundering-herd collisions) and over-waits on a one-off blip.

This module replaces the static window with a **dynamic full-jitter
exponential** window keyed to consecutive rupture *episodes*:

  * ``note_degraded()`` — register a rupture episode (debounced by ``base`` so a
    burst of ruptures inside one outage counts as ONE episode, not N).
  * ``note_recovered()`` — a DW completion succeeded → reset episodes to 0
    **instantly** (a transient blip recovers fast).
  * ``dynamic_recovery_window_s()`` — the lane stays severed for this long
    before the next probe: ``max(base, full_jitter_delay(episode-1, base, cap))``.
    Episode 1 → ``base``; episode N → up to ``base·2^(N-1)`` (jittered), capped.

The backoff math is **composed** from the EXISTING AWS full-jitter primitive
(``circuit_breaker.full_jitter_delay``) — no duplicate algorithm. Thread-safe
process singleton (modeled on ``dual_lane_breaker``). Pure, env-driven, NEVER
raises.

Master ``JARVIS_DW_DYNAMIC_RECOVERY_ENABLED`` (default **FALSE**, §33.1): when
OFF, callers fall back to the static window (byte-identical to pre-Slice-127-P3).
Knobs: ``JARVIS_DW_RECOVERY_BASE_S`` (default 30.0), ``JARVIS_DW_RECOVERY_CAP_S``
(default 600.0).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from backend.core.ouroboros.governance.circuit_breaker import full_jitter_delay

_ENV_MASTER = "JARVIS_DW_DYNAMIC_RECOVERY_ENABLED"
_ENV_BASE_S = "JARVIS_DW_RECOVERY_BASE_S"
_ENV_CAP_S = "JARVIS_DW_RECOVERY_CAP_S"

_DEFAULT_BASE_S = 30.0
_DEFAULT_CAP_S = 600.0


def dw_dynamic_recovery_enabled() -> bool:
    """Master gate. Slice 146: graduated default-TRUE (DW transport self-healing
    on by default — live-proven). NEVER raises."""
    try:
        return os.getenv(_ENV_MASTER, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def _base_s() -> float:
    try:
        v = float(os.getenv(_ENV_BASE_S, "").strip() or _DEFAULT_BASE_S)
        return v if v > 0 else _DEFAULT_BASE_S
    except (TypeError, ValueError):
        return _DEFAULT_BASE_S


def _cap_s() -> float:
    try:
        v = float(os.getenv(_ENV_CAP_S, "").strip() or _DEFAULT_CAP_S)
        return v if v > 0 else _DEFAULT_CAP_S
    except (TypeError, ValueError):
        return _DEFAULT_CAP_S


class DWTransportRecovery:
    """Thread-safe consecutive-episode tracker + dynamic recovery window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._episode_count: int = 0
        self._last_degraded_mono: Optional[float] = None

    @property
    def episode_count(self) -> int:
        with self._lock:
            return self._episode_count

    def note_degraded(self, now: Optional[float] = None) -> None:
        """Register a rupture. Debounced by ``base``: ruptures within ``base``
        seconds of the previous one belong to the SAME outage (one episode);
        a rupture after a longer gap is a NEW episode. NEVER raises."""
        ts = time.monotonic() if now is None else float(now)
        try:
            with self._lock:
                last = self._last_degraded_mono
                if last is None or (ts - last) > _base_s():
                    self._episode_count += 1
                self._last_degraded_mono = ts
        except Exception:  # noqa: BLE001 — sits on the dispatch error path
            pass

    def note_recovered(self) -> None:
        """A DW completion succeeded — reset episodes to 0 instantly so the
        next blip recovers at ``base``. NEVER raises."""
        try:
            with self._lock:
                self._episode_count = 0
                self._last_degraded_mono = None
        except Exception:  # noqa: BLE001
            pass

    def dynamic_recovery_window_s(self, rng: Optional[Any] = None) -> float:
        """Seconds the DW lane stays severed before the next probe.

        0 episodes → 0.0 (lane healthy, no window). Otherwise
        ``max(base, full_jitter_delay(episode-1, base, cap))`` — episode 1 is
        exactly ``base``; higher episodes back off exponentially (jittered,
        capped). NEVER raises (degrades to ``base`` on any error)."""
        with self._lock:
            episodes = self._episode_count
        if episodes <= 0:
            return 0.0
        base = _base_s()
        try:
            jittered = full_jitter_delay(
                max(0, episodes - 1), base_s=base, cap_s=_cap_s(), rng=rng,
            )
            return max(base, float(jittered))
        except Exception:  # noqa: BLE001 — never starve recovery on a math error
            return base

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "episode_count": self._episode_count,
                "last_degraded_mono": self._last_degraded_mono,
                "base_s": _base_s(),
                "cap_s": _cap_s(),
                "enabled": dw_dynamic_recovery_enabled(),
            }

    def reset(self) -> None:
        """Tests / new session."""
        with self._lock:
            self._episode_count = 0
            self._last_degraded_mono = None


# Process-wide singleton — lazy, side-effect-free import (mirrors
# dual_lane_breaker). candidate_generator notes degraded/recovered; the
# preflight gate reads the dynamic window.
_SINGLETON: "DWTransportRecovery | None" = None
_SINGLETON_LOCK = threading.Lock()


def get_dw_transport_recovery() -> DWTransportRecovery:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = DWTransportRecovery()
    return _SINGLETON


def reset_dw_transport_recovery() -> None:
    """Test isolation — reset the singleton's counters."""
    get_dw_transport_recovery().reset()


__all__ = [
    "DWTransportRecovery",
    "dw_dynamic_recovery_enabled",
    "get_dw_transport_recovery",
    "reset_dw_transport_recovery",
]
