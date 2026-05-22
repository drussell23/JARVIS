"""Control-plane starvation watchdog — Slice 11 Phase 11A.

Empirical context: bt-2026-05-22-011927 (Slice 7g acceptance soak).
After Slice 10 isolated ChromaDB, the asyncio event loop STILL
starved — sample showed the main thread blocked in
``builtin_compile → gc_collect_main`` for many seconds at a time.
The session debug.log froze for 6+ minutes because the loop
couldn't tick to emit anything.

This module is the EARLY-WARNING SIGNAL for that kind of
starvation: an independent watchdog task that schedules a
short ``asyncio.sleep`` repeatedly and measures the actual elapsed
wall-clock time vs the requested sleep. When the delta exceeds a
threshold, the loop is starved — log it loudly with
``[ControlPlaneStarvation]`` so operators see the wedge in real
time instead of after the fact.

## Discipline (Phase 11A — instrumentation only)

  * **Pure telemetry** — never modifies any other coroutine's
    behavior. Just measures and logs.
  * **Bounded resource** — single asyncio task, ~50 byte payload
    per tick. Negligible.
  * **NEVER raises** — every measurement is wrapped; instrumentation
    failure preserves system behavior.
  * **Configurable** — env knobs control cadence + threshold so
    operators can dial sensitivity per environment.
  * **Records ring** — bounded in-memory history for forensic
    inspection alongside ``ast_compile_telemetry`` records.

## API

  * ``ControlPlaneWatchdog`` — class. ``.start()`` / ``.stop()``.
  * ``get_default_watchdog()`` — process-singleton accessor (for
    harness boot wiring + observability endpoint).
  * ``recent_lag_records()`` — snapshot for forensics.

## Slice 11B handoff

When Slice 11A surfaces a caller that consistently triggers
``[ControlPlaneStarvation] lag_ms=X`` events, Slice 11B's
canonical helper (``ast_compile_helper`` / ``code_analysis_worker``)
will route that caller off the main control plane. The watchdog
provides the empirical proof that the refactor actually
eliminated the starvation."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional


logger = logging.getLogger("Ouroboros.ControlPlaneWatchdog")


# ============================================================================
# Env knobs
# ============================================================================


_MASTER_FLAG_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_ENABLED"
_INTERVAL_S_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_INTERVAL_S"
_THRESHOLD_MS_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_THRESHOLD_MS"
_RING_CAP_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_RING_CAP"

_DEFAULT_INTERVAL_S: float = 0.1   # 100ms — high-resolution
_MIN_INTERVAL_S: float = 0.01
_MAX_INTERVAL_S: float = 5.0
_DEFAULT_THRESHOLD_MS: float = 500.0  # half a second of lag is alarming
_MIN_THRESHOLD_MS: float = 10.0
_MAX_THRESHOLD_MS: float = 60_000.0
_DEFAULT_RING_CAP: int = 256


def watchdog_enabled() -> bool:
    """Master gate. Default TRUE for Phase 11A. Explicit
    ``"false"`` opts out. NEVER raises."""
    try:
        return os.environ.get(_MASTER_FLAG_ENV, "").strip().lower() not in (
            "0", "false", "no", "off",
        )
    except Exception:  # noqa: BLE001
        return True


def _resolve_interval_s() -> float:
    try:
        raw = os.environ.get(_INTERVAL_S_ENV, "").strip()
        if not raw:
            return _DEFAULT_INTERVAL_S
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, min(_MAX_INTERVAL_S, v))


def _resolve_threshold_ms() -> float:
    try:
        raw = os.environ.get(_THRESHOLD_MS_ENV, "").strip()
        if not raw:
            return _DEFAULT_THRESHOLD_MS
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD_MS
    return max(_MIN_THRESHOLD_MS, min(_MAX_THRESHOLD_MS, v))


def _resolve_ring_cap() -> int:
    try:
        raw = os.environ.get(_RING_CAP_ENV, "").strip()
        if not raw:
            return _DEFAULT_RING_CAP
        return max(16, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_RING_CAP


# ============================================================================
# Lag record
# ============================================================================


@dataclass(frozen=True)
class LagRecord:
    """One starvation observation."""

    lag_ms: float          # observed - requested
    requested_ms: float    # the configured interval
    observed_ms: float     # actual wall-clock elapsed
    ts_monotonic: float    # event time
    thread_name: str       # main thread (asyncio loop) at instrumentation point


# ============================================================================
# Watchdog
# ============================================================================


class ControlPlaneWatchdog:
    """Independent asyncio task that detects event-loop lag.

    Lifecycle:
      * ``start()`` — sync; schedules the watchdog task. NEVER
        raises. Returns False when master flag is off OR there's
        no running loop.
      * ``stop()`` — async; cancels the watchdog cleanly with a
        bounded ``wait_for``. NEVER raises.

    The watchdog loop is shaped like::

        while running:
            t0 = time.monotonic()
            await asyncio.sleep(interval_s)
            elapsed = time.monotonic() - t0
            lag = elapsed - interval_s
            if lag * 1000.0 >= threshold_ms:
                logger.warning("[ControlPlaneStarvation] lag_ms=%.1f ...", ...)

    The ``await asyncio.sleep`` is the canonical asyncio probe —
    when the loop is healthy the elapsed ≈ interval. When the loop
    is starved (some coroutine doing sync CPU work blocking the
    GIL), ``sleep`` returns LATE because the loop wasn't ticking
    to honor the timer."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        threshold_ms: Optional[float] = None,
    ) -> None:
        self._interval_s = (
            interval_s if interval_s is not None
            else _resolve_interval_s()
        )
        self._threshold_ms = (
            threshold_ms if threshold_ms is not None
            else _resolve_threshold_ms()
        )
        self._ring: Deque[LagRecord] = deque(maxlen=_resolve_ring_cap())
        self._ring_lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._lag_event_count: int = 0  # number of lag-threshold breaches

    # ---- introspection ----

    @property
    def interval_s(self) -> float:
        return self._interval_s

    @property
    def threshold_ms(self) -> float:
        return self._threshold_ms

    @property
    def lag_event_count(self) -> int:
        return self._lag_event_count

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def recent_lag_records(
        self, limit: Optional[int] = None,
    ) -> List[LagRecord]:
        with self._ring_lock:
            items = list(self._ring)
        if limit is not None:
            items = items[-int(limit):]
        return items

    # ---- lifecycle ----

    def start(self) -> bool:
        """Spawn the watchdog task. No-op when master flag is
        FALSE or no running loop. Returns True when the task was
        spawned."""
        if not watchdog_enabled():
            logger.debug(
                "[ControlPlaneWatchdog] master flag FALSE — "
                "watchdog not started"
            )
            return False
        if self.running:
            return False
        try:
            self._task = asyncio.create_task(
                self._run(),
                name="control_plane_watchdog",
            )
            logger.info(
                "[ControlPlaneWatchdog] started interval=%.3fs "
                "threshold_ms=%.0f",
                self._interval_s, self._threshold_ms,
            )
            return True
        except RuntimeError:
            logger.debug(
                "[ControlPlaneWatchdog] start: no running loop"
            )
            return False

    async def stop(self) -> None:
        """Cancel cleanly. NEVER raises. Bounded by a short
        wait_for."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 — never raise
            logger.debug(
                "[ControlPlaneWatchdog] stop: exception ignored",
            )
        finally:
            self._task = None
            logger.info(
                "[ControlPlaneWatchdog] stopped lag_events=%d",
                self._lag_event_count,
            )

    # ---- the watchdog loop ----

    async def _run(self) -> None:
        """Sleeps for ``interval_s``, measures actual elapsed,
        logs when lag exceeds threshold. Independent of any other
        coroutine."""
        while True:
            try:
                t0 = time.monotonic()
                await asyncio.sleep(self._interval_s)
                elapsed = time.monotonic() - t0
                lag_s = elapsed - self._interval_s
                lag_ms = lag_s * 1000.0
                observed_ms = elapsed * 1000.0
                requested_ms = self._interval_s * 1000.0
                try:
                    with self._ring_lock:
                        self._ring.append(LagRecord(
                            lag_ms=lag_ms,
                            requested_ms=requested_ms,
                            observed_ms=observed_ms,
                            ts_monotonic=t0,
                            thread_name=threading.current_thread().name,
                        ))
                except Exception:  # noqa: BLE001
                    pass
                if lag_ms >= self._threshold_ms:
                    self._lag_event_count += 1
                    logger.warning(
                        "[ControlPlaneStarvation] lag_ms=%.1f "
                        "(requested=%.1f observed=%.1f) "
                        "threshold=%.1f event_n=%d — main asyncio "
                        "loop is starved",
                        lag_ms, requested_ms, observed_ms,
                        self._threshold_ms, self._lag_event_count,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never tear down
                # Log + continue. A single bad cycle must not
                # crash the watchdog.
                logger.debug(
                    "[ControlPlaneWatchdog] cycle exception ignored",
                    exc_info=True,
                )
                # Yield briefly so we don't tight-loop on a
                # persistent error.
                try:
                    await asyncio.sleep(self._interval_s)
                except asyncio.CancelledError:
                    raise


# ============================================================================
# Process-singleton accessor
# ============================================================================


_default_watchdog: Optional[ControlPlaneWatchdog] = None
_default_lock = threading.Lock()


def get_default_watchdog() -> ControlPlaneWatchdog:
    """Process-singleton accessor. NEVER raises."""
    global _default_watchdog
    with _default_lock:
        if _default_watchdog is None:
            _default_watchdog = ControlPlaneWatchdog()
        return _default_watchdog


def reset_default_watchdog() -> None:
    """For tests."""
    global _default_watchdog
    with _default_lock:
        _default_watchdog = None


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "ControlPlaneWatchdog",
    "LagRecord",
    "watchdog_enabled",
    "get_default_watchdog",
    "reset_default_watchdog",
]
