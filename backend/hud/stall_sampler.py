"""Catch the event loop being blocked, while it is still blocked.

WHY THIS EXISTS
---------------
`LoopSentinel` reports the stalls perfectly — 6.71s, 8.05s, 7.66s during a
single boot on 2026-08-05, availability 41-65%. What it cannot report is WHO,
and for a structural reason rather than an oversight: it is a task ON the loop
it measures. While the loop is dead it is not running, so by the time it wakes
to record the lag, whatever held the loop has already let go. It is the
victim, and the victim never sees the culprit.

Every attempt to name that culprit from inside the loop has the same flaw, and
one of them cost most of a day: `loop_sink.sink_async` measures wall-clock and
was read as blocking time, which pointed an investigation at
`posture_observer` — a subsystem that was already off-loop and entirely
innocent.

So this samples from a THREAD. When the heartbeat the sentinel publishes goes
stale, the loop is wedged AT THIS INSTANT, and the stacks are dumped while it
is still wedged. That is the difference between "something blocked the loop
for eight seconds" and "here is the function that did it".

The separation is the same one that makes the parent watch work, and the same
one the battle harness's wall-clock watchdog is forbidden from breaking: an
observer that shares a resource with the thing it observes is not an observer.
This thread reads one float and calls `faulthandler`; it takes no lock the
loop can hold, allocates nothing the loop can starve, and asks the application
for nothing.

WHY faulthandler AND NOT traceback
------------------------------------
`faulthandler.dump_traceback` writes from C, walking interpreter state without
executing Python and without taking the logging lock. A thread blocked inside
a C call — a lock acquire, a `read()`, a futex wait — runs no bytecode, so
anything Python-level is deferred until it moves, which is exactly never in
the case being investigated. Reused from `oob_diagnostics.dump_all_threads`
rather than reimplemented; that module already solved this for the `/` freeze.

BOUNDED, BECAUSE A DIAGNOSTIC MUST NOT BECOME THE INCIDENT
------------------------------------------------------------
Boot produces many stalls in a short window. Dumping every one would write
megabytes and add its own I/O to a machine already under memory pressure. So:
a minimum gap between dumps, a hard cap per process, and each dump is one
write. When the cap is reached it says so once and stops — silence would look
like health.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("jarvis.stallsampler")

STALL_SAMPLER_SCHEMA_VERSION: str = "stall_sampler.v1"

#: Master switch. Default TRUE — it costs one float read per interval while
#: the loop is healthy, and the thing it catches is otherwise uncatchable.
ENV_ENABLED: str = "JARVIS_STALL_SAMPLER_ENABLED"

#: How stale the heartbeat must be before the loop counts as wedged NOW.
#: Deliberately larger than the sentinel's own stall threshold: this is for
#: the pathological windows worth a stack dump, not every scheduling hiccup.
ENV_TRIGGER_S: str = "JARVIS_STALL_SAMPLER_TRIGGER_S"

#: Minimum gap between dumps, so one long stall yields one dump.
ENV_MIN_GAP_S: str = "JARVIS_STALL_SAMPLER_MIN_GAP_S"

#: Hard cap per process.
ENV_MAX_DUMPS: str = "JARVIS_STALL_SAMPLER_MAX_DUMPS"


def _enabled() -> bool:
    return (os.environ.get(ENV_ENABLED, "true") or "").strip().lower() not in (
        "0", "false", "no", "off")


def _f(name: str, default: float, minimum: float) -> float:
    """Read a knob, clamped. NEVER raises.

    Clamped rather than rejected: a typo in a diagnostic's timing must not be
    able to turn it into a spin loop on a machine that is already struggling.
    """
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return max(minimum, float(raw)) if raw else default
    except Exception:  # noqa: BLE001
        return default


def trigger_s() -> float:
    return _f(ENV_TRIGGER_S, 2.0, 0.25)


def min_gap_s() -> float:
    return _f(ENV_MIN_GAP_S, 10.0, 1.0)


def max_dumps() -> int:
    return int(_f(ENV_MAX_DUMPS, 8.0, 1.0))


class StallSampler:
    """Dumps every thread's stack while the loop is unresponsive. NEVER raises."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._dumps = 0
        self._last_dump = 0.0
        self._capped_announced = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Arm the sampler on a daemon thread. Idempotent. NEVER raises."""
        try:
            if not _enabled():
                logger.info("[StallSampler] disabled by %s", ENV_ENABLED)
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="loop-stall-sampler", daemon=True)
            self._thread.start()
            logger.info(
                "[StallSampler] armed — a loop wedged for more than %.1fs will "
                "have its stacks captured WHILE wedged (max %d dumps, %.0fs "
                "apart). The sentinel says a stall happened; this says what "
                "was running.",
                trigger_s(), max_dumps(), min_gap_s())
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[StallSampler] start degraded", exc_info=True)
            return False

    def stop(self) -> None:
        self._stop.set()

    # -- the watch -------------------------------------------------------

    def _heartbeat(self) -> float:
        """The sentinel's last tick, or 0.0 if it is not running.

        Read without a lock on purpose. A float read is atomic under CPython,
        and taking a lock the loop might hold would let this thread block on
        exactly the condition it exists to observe.
        """
        try:
            from backend.hud import loop_sentinel as ls
            s = ls._SENTINEL
            return float(getattr(s, "_last_tick", 0.0)) if s is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _run(self) -> None:
        # Sample considerably faster than the trigger so a stall is caught
        # near its beginning rather than near its end — a dump taken as the
        # loop recovers shows the recovery, not the cause.
        while not self._stop.is_set():
            interval = max(0.1, trigger_s() / 4.0)
            if self._stop.wait(interval):
                return
            try:
                beat = self._heartbeat()
                if beat <= 0.0:
                    continue          # sentinel not started; nothing to judge
                stale_for = time.monotonic() - beat
                if stale_for < trigger_s():
                    continue
                now = time.monotonic()
                if now - self._last_dump < min_gap_s():
                    continue          # same stall, already captured
                if self._dumps >= max_dumps():
                    if not self._capped_announced:
                        self._capped_announced = True
                        logger.warning(
                            "[StallSampler] dump cap (%d) reached — further "
                            "stalls will NOT be captured. Raise %s if the "
                            "culprit has not shown up yet.",
                            max_dumps(), ENV_MAX_DUMPS)
                    continue
                self._last_dump = now
                self._dumps += 1
                self._capture(stale_for)
            except Exception:  # noqa: BLE001 — a witness never dies of a fault
                logger.debug("[StallSampler] sample degraded", exc_info=True)

    def _capture(self, stale_for: float) -> None:
        """One dump, through the module that already does this correctly."""
        logger.warning(
            "[StallSampler] LOOP WEDGED for %.2fs RIGHT NOW — capturing "
            "stacks (dump %d/%d). The frames below are what the loop is "
            "stuck on, not what it ran afterwards.",
            stale_for, self._dumps, max_dumps())
        try:
            from backend.core.ouroboros.battle_test.oob_diagnostics import (
                dump_all_threads,
            )
            if dump_all_threads():
                return
        except Exception:  # noqa: BLE001
            logger.debug("[StallSampler] oob dump unavailable", exc_info=True)
        # Fallback: straight to stderr. Less convenient than the log file the
        # oob module maintains, and infinitely better than nothing at the one
        # moment the answer exists.
        try:
            import faulthandler
            faulthandler.dump_traceback(all_threads=True)
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> dict:
        return {
            "schema_version": STALL_SAMPLER_SCHEMA_VERSION,
            "enabled": _enabled(),
            "running": bool(self._thread and self._thread.is_alive()),
            "dumps": self._dumps,
            "max_dumps": max_dumps(),
            "trigger_s": trigger_s(),
        }


_SAMPLER: Optional[StallSampler] = None


def get_stall_sampler() -> StallSampler:
    """Process-wide sampler. NEVER raises."""
    global _SAMPLER
    if _SAMPLER is None:
        _SAMPLER = StallSampler()
    return _SAMPLER


def reset_stall_sampler() -> None:
    """Testing seam."""
    global _SAMPLER
    if _SAMPLER is not None:
        _SAMPLER.stop()
    _SAMPLER = None
