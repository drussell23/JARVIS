"""Harness Epic Slice 1 — BoundedShutdownWatchdog.

Guarantees that every battle-test session terminates within a bounded
deadline, regardless of asyncio event-loop state. Closes the rooted
problem documented in ``project_followup_battle_test_post_summary_hang.md``:

* 14 incidents of ``Py_FinalizeEx → PyThread_acquire_lock_timed →
  __psynch_cvwait`` deadlock during interpreter shutdown.
* SIGTERM-during-steady-state failing to write ``summary.json`` (S5/S6).
* ``WallClockWatchdog`` not firing at ``max_wall_seconds`` (S6 — asyncio
  task starvation hypothesis).

All three classes have the same root cause: shutdown discipline that
depends on the asyncio event loop being responsive. When the loop is
wedged, the asyncio-side termination paths can't fire — and Python's
own ``Py_FinalizeEx`` deadlocks on non-daemon thread joins.

This module provides a **synchronous-thread-based** escape hatch.

Architecture:

* A daemon thread is spawned at harness init. It blocks on a
  ``threading.Event`` until ``arm()`` is called.
* On ``arm(reason, deadline_s)``, the thread wakes, sleeps
  ``deadline_s``, then calls ``os._exit(EXIT_CODE_HARNESS_WEDGED=75)``.
  ``os._exit`` does NOT run cleanup handlers, atexit, or finalizers —
  it terminates the process immediately at the C level.
* If clean shutdown completes before the deadline, ``disarm()`` clears
  the event and the thread re-blocks. No ``os._exit`` fires.
* Daemon thread → no ``Py_FinalizeEx`` join blocking → interpreter can
  exit cleanly via the asyncio path when that path works.

Master flag: ``JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED`` (default
``true``). Hot-revert: ``=false`` reverts to pre-Slice-1 (asyncio-only
shutdown — old behavior). Defaulting ``true`` is safe because the
watchdog is ``disarm()``-able; clean shutdowns don't trigger
``os._exit``.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Callable, Optional


logger = logging.getLogger("Ouroboros.ShutdownWatchdog")


# Reserved exit code for "harness wedged, os._exit fired". 75 = EX_TEMPFAIL
# in BSD sysexits.h — operationally appropriate (try-again-later semantics).
# Distinct from 0 (clean) and 1 (generic error) so wrappers can detect
# wedge-vs-error without log parsing.
EXIT_CODE_HARNESS_WEDGED: int = 75


def _env_bool(name: str, default: bool) -> bool:
    """Standard JARVIS env-bool parse — true/1/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def bounded_shutdown_enabled() -> bool:
    """Master flag — `JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED` (default true).

    Post-Slice-1 graduation, defaults true. Hot-revert: ``=false`` reverts
    to pre-Slice-1 (asyncio-only shutdown). Safe to default true because
    the watchdog is ``disarm()``-able; clean shutdowns don't trigger
    ``os._exit``.
    """
    return _env_bool("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", True)


def default_deadline_s() -> float:
    """`JARVIS_BATTLE_SHUTDOWN_DEADLINE_S` — bounded shutdown budget.

    Default 30s. Sized for the worst-case clean-shutdown chain: stop GLS
    (~5s) + flush durables (~5s) + write summary.json (~2s) + GC + asyncio
    teardown (~5s) + slack. If exceeded, ``os._exit`` fires.
    """
    raw = os.environ.get("JARVIS_BATTLE_SHUTDOWN_DEADLINE_S", "30.0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 30.0


class BoundedShutdownWatchdog:
    """Daemon-thread-based deadline watchdog for harness shutdown.

    Lifecycle:
      1. ``__init__()`` — spawns the daemon thread. Thread is idle (blocked
         on the arm event) until ``arm()`` is called.
      2. ``arm(reason, deadline_s)`` — wakes the thread; deadline starts.
      3. ``disarm()`` — clears the event so a subsequent ``arm()`` re-arms
         cleanly. If the thread is currently in its post-arm sleep, the
         disarm event interrupts it and the thread re-blocks.
      4. If deadline elapses with arm still held, ``os._exit(75)`` fires.

    Idempotency:
      * Multiple ``arm()`` calls — first wins (records first reason), later
        calls are no-ops. Avoids accidentally extending the deadline.
      * Multiple ``disarm()`` calls — idempotent.
      * ``arm()`` after ``disarm()`` — re-arms cleanly with new reason.

    Test affordances:
      * ``exit_fn`` constructor kwarg — defaults to ``os._exit``. Tests
        inject a recorder so the deadline-elapse path is observable
        without actually exiting.
      * ``sleep_fn`` constructor kwarg — defaults to ``time.sleep``. Tests
        inject a fast-clock to verify deadline math without real-time waits.
    """

    def __init__(
        self,
        *,
        exit_fn: Callable[[int], None] = os._exit,
        sleep_fn: Callable[[float], None] = time.sleep,
        thread_name: str = "BoundedShutdownWatchdog",
    ) -> None:
        self._exit_fn = exit_fn
        self._sleep_fn = sleep_fn
        # The arm event signals "shutdown requested; deadline running".
        self._arm_event = threading.Event()
        # The disarm event lets disarm() interrupt a sleeping thread.
        self._disarm_event = threading.Event()
        # Stop-thread event for clean teardown in tests.
        self._stop_event = threading.Event()
        # State (only mutated under _lock)
        self._lock = threading.Lock()
        self._reason: Optional[str] = None
        self._deadline_s: float = 0.0
        self._armed_at_monotonic: Optional[float] = None
        self._fired: bool = False
        # Daemon thread — won't block Py_FinalizeEx
        self._thread = threading.Thread(
            target=self._thread_loop,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()

    @property
    def is_armed(self) -> bool:
        return self._arm_event.is_set() and not self._disarm_event.is_set()

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    @property
    def deadline_s(self) -> float:
        with self._lock:
            return self._deadline_s

    @property
    def armed_at_monotonic(self) -> Optional[float]:
        with self._lock:
            return self._armed_at_monotonic

    @property
    def fired(self) -> bool:
        return self._fired

    def arm(self, reason: str, deadline_s: float) -> bool:
        """Start the deadline. Returns True on first arm, False if already armed.

        First-arm-wins semantics: if armed already, the existing deadline
        + reason are preserved. This avoids accidentally extending the
        deadline by re-arming.
        """
        if not bounded_shutdown_enabled():
            return False
        with self._lock:
            if self._arm_event.is_set() and not self._disarm_event.is_set():
                # Already armed — first wins
                logger.info(
                    "[ShutdownWatchdog] arm() ignored — already armed reason=%r "
                    "deadline_s=%.1f (requested reason=%r deadline_s=%.1f)",
                    self._reason, self._deadline_s, reason, deadline_s,
                )
                return False
            self._reason = reason
            self._deadline_s = max(0.0, float(deadline_s))
            self._armed_at_monotonic = time.monotonic()
            self._disarm_event.clear()
        # Set arm event AFTER state is committed so the thread sees the
        # right deadline when it wakes.
        self._arm_event.set()
        logger.warning(
            "[ShutdownWatchdog] ARMED reason=%r deadline_s=%.1f — "
            "os._exit(%d) will fire if not disarmed within deadline",
            reason, deadline_s, EXIT_CODE_HARNESS_WEDGED,
        )
        return True

    def disarm(self) -> bool:
        """Cancel the deadline. Returns True if was armed, False if already idle.

        Idempotent. Resets state so next ``arm()`` starts fresh.
        """
        with self._lock:
            if not self._arm_event.is_set():
                return False
            self._disarm_event.set()
            self._arm_event.clear()
            elapsed = (
                time.monotonic() - self._armed_at_monotonic
                if self._armed_at_monotonic is not None
                else 0.0
            )
            reason = self._reason
            deadline = self._deadline_s
        logger.info(
            "[ShutdownWatchdog] DISARMED reason=%r elapsed=%.2fs / deadline=%.1fs "
            "(clean shutdown completed within budget)",
            reason, elapsed, deadline,
        )
        return True

    def stop(self) -> None:
        """Signal the daemon thread to exit. Used in tests for clean teardown.

        Production daemon threads die with the interpreter; ``stop()`` is
        only needed when a test constructs a watchdog and wants to release
        the thread before the test ends.
        """
        self._stop_event.set()
        # Wake the thread if it's blocked
        self._arm_event.set()
        self._disarm_event.set()

    def _thread_loop(self) -> None:
        """Daemon thread body — wait, sleep deadline, fire os._exit."""
        while True:
            # Wait for arm or stop
            self._arm_event.wait()
            if self._stop_event.is_set():
                return
            # Snapshot deadline + reason under lock
            with self._lock:
                deadline = self._deadline_s
                reason = self._reason
            # Sleep deadline_s, but interruptible by disarm or stop.
            # Use the disarm event's wait(timeout=) — it blocks for up to
            # deadline_s and returns True if disarm fired.
            disarmed = self._disarm_event.wait(timeout=deadline)
            if self._stop_event.is_set():
                return
            if disarmed:
                # Clean shutdown beat the deadline — clear and loop
                self._disarm_event.clear()
                # Wait for re-arm (arm event was cleared by disarm())
                continue
            # Deadline elapsed without disarm — fire os._exit
            self._fired = True
            elapsed_at_fire = (
                time.monotonic() - self._armed_at_monotonic
                if self._armed_at_monotonic is not None
                else deadline
            )
            # Forensic line to stderr — bypasses logging which may itself
            # be wedged. Flush before _exit.
            try:
                sys.stderr.write(
                    f"\n[BoundedShutdownWatchdog] FIRED — "
                    f"reason={reason!r} elapsed={elapsed_at_fire:.1f}s "
                    f"deadline={deadline:.1f}s — calling "
                    f"os._exit({EXIT_CODE_HARNESS_WEDGED}) NOW\n"
                )
                sys.stderr.flush()
            except Exception:
                pass
            try:
                logger.error(
                    "[ShutdownWatchdog] FIRED reason=%r elapsed=%.1fs "
                    "deadline=%.1fs — os._exit(%d)",
                    reason, elapsed_at_fire, deadline,
                    EXIT_CODE_HARNESS_WEDGED,
                )
            except Exception:
                pass
            # GO
            self._exit_fn(EXIT_CODE_HARNESS_WEDGED)
            # exit_fn returned (only happens in tests with mocked exit) —
            # break out of loop and let stop()/test handle cleanup
            return


__all__ = [
    "BoundedShutdownWatchdog",
    "EXIT_CODE_HARNESS_WEDGED",
    "bounded_shutdown_enabled",
    "default_deadline_s",
]
