"""Async Loop Deadman Switch — Slice 12G-2.

Defends the asyncio control plane against catastrophic synchronous
wedges that even the Slice 11A ``ControlPlaneWatchdog`` cannot
report — because the watchdog itself depends on the loop being
able to tick. When the loop is fully wedged in sync work (e.g. a
catastrophic backtracking regex, an infinite parser loop, or a
runaway gc cycle), the asyncio-tick-based watchdog goes silent
and operators see no symptoms until the wall-cap Layer-3 SIGKILL
fires.

Empirical context (operator-flagged 2026-05-22): Phase 3A
relaunch wedged for 82 minutes at 187% CPU with the asyncio loop
fully dead. Last log entry at 13:07:57; Layer-3 SIGKILL at 14:30.
ControlPlaneWatchdog stopped firing the moment the wedge began —
the very signal we needed was silent for the entire duration.

## Architecture

  * **Daemon thread** — runs in an OS thread, NOT an asyncio task.
    Independent of loop liveness; survives any GIL-held block
    that's bounded (the GIL releases on time.sleep / I/O).
  * **Heartbeat protocol** — the asyncio loop periodically calls
    ``heartbeat()`` from an asyncio task. The deadman thread
    polls the last-heartbeat timestamp from its own thread.
  * **Bounded wedge ceiling** — when ``time.time() - last_heartbeat
    > deadman_timeout_s`` (default 300s = 5 min), the deadman
    fires:
      1. Log a structured ``[LoopDeadman] WEDGE DETECTED`` line.
      2. (Optional) Trigger a sample-stack-dump via ``faulthandler``
         and/or ``sample`` subprocess to capture forensic state.
      3. ``os._exit(75)`` — bypasses any wedged asyncio cleanup,
         the WallClockWatchdog Layer-3 race, and the atexit
         handlers (those would re-deadlock against the wedge).
        Exit code 75 distinguishes this from clean shutdown (0) +
        SIGKILL (-9) + wall_clock_cap (other paths).
  * **Pure stdlib** — ``threading``, ``time``, ``os``,
    ``faulthandler``, ``logging``. No new dependencies.

## What this does NOT do

  * Does NOT recover the wedge. Recovery is impossible by
    definition (the loop is dead). The deadman trades a 82-min
    silent wall-cap kill for a 5-min loud structured exit + a
    stack dump that lets the next iteration *fix* the wedge.
  * Does NOT replace ``WallClockWatchdog`` — that's the cap on
    total session duration; this is the cap on UNRESPONSIVE
    duration. Both fire structurally; both are independent.
  * Does NOT replace ``ControlPlaneWatchdog`` — that surfaces
    bursty starvation (single events of 100ms+ lag). This fires
    only on sustained total loop death.

## Env knobs

  * ``JARVIS_LOOP_DEADMAN_ENABLED``       — master gate (default TRUE).
  * ``JARVIS_LOOP_DEADMAN_TIMEOUT_S``     — wedge ceiling (default 300).
  * ``JARVIS_LOOP_DEADMAN_HEARTBEAT_S``   — async heartbeat cadence (default 5s).
  * ``JARVIS_LOOP_DEADMAN_STACK_DUMP``    — dump faulthandler stack to debug.log on fire (default TRUE).
  * ``JARVIS_LOOP_DEADMAN_SAMPLE``        — invoke macOS ``sample`` for richer forensics (default FALSE — slow + macOS-only).
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
from typing import Optional


logger = logging.getLogger("Ouroboros.LoopDeadman")


# ============================================================================
# Env-knob resolvers
# ============================================================================


def deadman_enabled() -> bool:
    """``JARVIS_LOOP_DEADMAN_ENABLED`` — default TRUE. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_LOOP_DEADMAN_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


def deadman_timeout_s() -> float:
    """Wedge ceiling — fire after this many seconds with no
    heartbeat. Default 300s (5 min). Floored at 30s, ceilinged at
    3600s to avoid pathological configurations."""
    try:
        raw = os.environ.get(
            "JARVIS_LOOP_DEADMAN_TIMEOUT_S", "",
        ).strip()
        v = float(raw) if raw else 300.0
        return max(30.0, min(3600.0, v))
    except (TypeError, ValueError):
        return 300.0


def deadman_heartbeat_s() -> float:
    """How often the async heartbeat task fires. Default 5s.
    Floored at 0.5s, ceilinged at 60s."""
    try:
        raw = os.environ.get(
            "JARVIS_LOOP_DEADMAN_HEARTBEAT_S", "",
        ).strip()
        v = float(raw) if raw else 5.0
        return max(0.5, min(60.0, v))
    except (TypeError, ValueError):
        return 5.0


def deadman_stack_dump_enabled() -> bool:
    """Dump faulthandler stack trace when the deadman fires.
    Default TRUE — the trace is the diagnostic gold we need to
    actually FIX the wedge on the next iteration."""
    raw = os.environ.get(
        "JARVIS_LOOP_DEADMAN_STACK_DUMP", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


# ============================================================================
# LoopDeadman
# ============================================================================


class LoopDeadman:
    """Daemon-thread monitor that fires ``os._exit(75)`` when the
    asyncio loop has been wedged in sync work past the configured
    ceiling. NEVER raises into the asyncio loop — the whole point
    is to operate from a thread the loop CANNOT influence."""

    __slots__ = (
        "_timeout_s", "_heartbeat_s", "_stack_dump",
        "_thread", "_async_task",
        "_last_heartbeat_at", "_lock",
        "_stop_event",
    )

    def __init__(
        self,
        *,
        timeout_s: Optional[float] = None,
        heartbeat_s: Optional[float] = None,
        stack_dump: Optional[bool] = None,
    ) -> None:
        self._timeout_s: float = (
            timeout_s if timeout_s is not None else deadman_timeout_s()
        )
        self._heartbeat_s: float = (
            heartbeat_s if heartbeat_s is not None
            else deadman_heartbeat_s()
        )
        self._stack_dump: bool = (
            stack_dump if stack_dump is not None
            else deadman_stack_dump_enabled()
        )
        self._thread: Optional[threading.Thread] = None
        self._async_task: Optional[asyncio.Task] = None
        self._last_heartbeat_at: float = time.time()
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()

    # ---- introspection ----

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def heartbeat_s(self) -> float:
        return self._heartbeat_s

    @property
    def running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    def last_heartbeat_age_s(self) -> float:
        with self._lock:
            return time.time() - self._last_heartbeat_at

    # ---- lifecycle ----

    def start(self) -> bool:
        """Start the deadman: spawn the daemon thread + schedule
        the async heartbeat task. NEVER raises. Returns True on
        success."""
        if not deadman_enabled():
            logger.info(
                "[LoopDeadman] disabled via JARVIS_LOOP_DEADMAN_ENABLED",
            )
            return False
        if self.running:
            return False
        with self._lock:
            self._last_heartbeat_at = time.time()
        # Daemon thread — OS thread, no asyncio dependency.
        self._thread = threading.Thread(
            target=self._run_deadman_loop,
            name="LoopDeadman",
            daemon=True,
        )
        self._thread.start()
        # Async heartbeat task — pings the timestamp every
        # heartbeat_s when the asyncio loop is healthy.
        try:
            loop = asyncio.get_running_loop()
            self._async_task = loop.create_task(
                self._run_heartbeat_loop(),
                name="loop_deadman_heartbeat",
            )
        except RuntimeError:
            logger.debug(
                "[LoopDeadman] start: no running loop — heartbeat "
                "task not scheduled (deadman still active)",
            )
        logger.info(
            "[LoopDeadman] armed: timeout=%.0fs heartbeat=%.1fs "
            "stack_dump=%s — fires os._exit(75) on wedge detection",
            self._timeout_s, self._heartbeat_s, self._stack_dump,
        )
        return True

    async def stop(self) -> None:
        """Cancel cleanly. NEVER raises. Daemon thread is GC'd
        with the process; we just clear the flag so the
        ``_run_deadman_loop`` exits at its next poll."""
        self._stop_event.set()
        if self._async_task is not None and not self._async_task.done():
            self._async_task.cancel()
            try:
                await asyncio.wait_for(
                    self._async_task, timeout=2.0,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            "[LoopDeadman] stopped — last heartbeat was %.1fs ago",
            self.last_heartbeat_age_s(),
        )

    def heartbeat(self) -> None:
        """Public API — call from the asyncio loop to prove
        liveness. Thread-safe. NEVER raises."""
        with self._lock:
            self._last_heartbeat_at = time.time()

    # ---- the daemon thread loop ----

    def _run_deadman_loop(self) -> None:
        """Polls the heartbeat age from the daemon thread. On wedge
        detection, logs + dumps stack + ``os._exit(75)``."""
        while not self._stop_event.is_set():
            try:
                age = self.last_heartbeat_age_s()
                if age > self._timeout_s:
                    self._fire_wedge(age)
                    return  # unreachable in practice (os._exit fires)
                # Sleep half the timeout so wedge detection latency
                # is bounded.
                time.sleep(min(self._heartbeat_s, self._timeout_s / 4.0))
            except Exception:  # noqa: BLE001 — never crash deadman
                # If our own polling raises, sleep a beat + retry
                # rather than tear down the monitor.
                try:
                    time.sleep(1.0)
                except Exception:  # noqa: BLE001
                    pass

    # ---- the asyncio heartbeat task ----

    async def _run_heartbeat_loop(self) -> None:
        """asyncio task that pings ``heartbeat()`` every
        ``heartbeat_s``. When the loop is healthy, the deadman
        thread sees fresh timestamps. When the loop wedges, the
        timestamp goes stale — the deadman thread detects this
        from outside."""
        try:
            while not self._stop_event.is_set():
                self.heartbeat()
                await asyncio.sleep(self._heartbeat_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[LoopDeadman] heartbeat task error (handled)",
                exc_info=True,
            )

    # ---- wedge detection + fire ----

    def _fire_wedge(self, wedge_age_s: float) -> None:
        """Wedge detected — log + (optional) stack dump +
        ``os._exit(75)``. Best-effort logging; the exit MUST
        happen regardless."""
        try:
            logger.critical(
                "[LoopDeadman] WEDGE DETECTED: asyncio loop has "
                "not heartbeat for %.1fs (timeout=%.0fs). Firing "
                "os._exit(75) — bypassing asyncio cleanup + "
                "atexit (those would re-deadlock against the "
                "wedge). Stack dump to follow if enabled.",
                wedge_age_s, self._timeout_s,
            )
        except Exception:  # noqa: BLE001 — even logging can wedge
            pass
        if self._stack_dump:
            try:
                # Dump every thread's stack to stderr (faulthandler
                # is signal-safe and works from any thread). The
                # harness's stderr capture lands this in debug.log.
                faulthandler.dump_traceback(file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
        # Final, unrecoverable exit. ``os._exit`` bypasses Python
        # cleanup (atexit handlers, asyncio __del__, etc.) — those
        # would re-deadlock on the wedge. The session manager's
        # next boot will see exit code 75 and can route diagnostics
        # accordingly.
        os._exit(75)


# ============================================================================
# Process-singleton accessor
# ============================================================================


_default_deadman: Optional[LoopDeadman] = None
_default_lock: threading.Lock = threading.Lock()


def get_default_deadman() -> LoopDeadman:
    """Process-singleton accessor. NEVER raises."""
    global _default_deadman
    with _default_lock:
        if _default_deadman is None:
            _default_deadman = LoopDeadman()
        return _default_deadman


def reset_default_deadman() -> None:
    """For tests."""
    global _default_deadman
    with _default_lock:
        _default_deadman = None


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "LoopDeadman",
    "deadman_enabled",
    "deadman_timeout_s",
    "deadman_heartbeat_s",
    "deadman_stack_dump_enabled",
    "get_default_deadman",
    "reset_default_deadman",
]
