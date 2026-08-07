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
ENV_DUMP_PATH = "JARVIS_STALL_DUMP_PATH"
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



def _now_iso() -> str:
    """Timestamp for a dump header. NEVER raises."""
    try:
        from datetime import datetime
        return datetime.now().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return "unknown-time"


def _dump_path():
    """Where stall stacks are written so they outlive the process.

    Env-overridable; defaults alongside the other JARVIS logs so an operator
    looking for "what was the loop stuck on" finds it where the boot log lives.
    Returns None if a path cannot be resolved, and the caller falls back to
    stderr rather than losing the dump.
    """
    try:
        from pathlib import Path as _Path
        raw = (os.environ.get(ENV_DUMP_PATH, "") or "").strip()
        if raw:
            return _Path(raw).expanduser()
        return _Path.home() / "Library" / "Logs" / "JARVIS" / "loop-stalls.log"
    except Exception:  # noqa: BLE001
        return None


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
        # WRITE THE FRAMES WHERE THEY CAN BE READ AFTERWARDS.
        #
        # `dump_all_threads()` defaults to sys.stderr, and the brainstem console
        # filters the backend's output to a whitelist of prefixes. A raw
        # faulthandler traceback matches none of them, so the stacks this
        # sampler exists to capture have been discarded at the console for every
        # run — the message "the frames below are what the loop is stuck on"
        # was followed by nothing the operator could see.
        #
        # That is why loop starvation is the one defect still open after the
        # rest of this chain was fixed: the instrument built to name the starver
        # was writing to a stream nobody reads. Same lesson as BootLogFile on
        # the Swift side, and as the four other observability findings in this
        # arc — a measurement that cannot be recovered is not a measurement.
        #
        # The dump goes to a file that outlives the process. stderr is kept as
        # well: a developer watching live should not lose what they had.
        dump_target = None
        try:
            path = _dump_path()
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                dump_target = path.open("a", encoding="utf-8")
                dump_target.write(
                    f"\n===== STALL DUMP {self._dumps}/{max_dumps()} — "
                    f"loop wedged {stale_for:.2f}s — "
                    f"{_now_iso()} =====\n"
                )
                dump_target.flush()
                logger.warning("[StallSampler] frames -> %s", path)
        except Exception:  # noqa: BLE001 — a witness never dies of a fault
            dump_target = None

        try:
            from backend.core.ouroboros.battle_test.oob_diagnostics import (
                dump_all_threads,
            )
            # The file first: it is the copy that survives. stderr second, for
            # whoever is attached right now.
            wrote = False
            if dump_target is not None:
                wrote = bool(dump_all_threads(file=dump_target))
            if dump_all_threads() or wrote:
                return
        except Exception:  # noqa: BLE001
            logger.debug("[StallSampler] oob dump unavailable", exc_info=True)
        finally:
            if dump_target is not None:
                try:
                    dump_target.flush()
                    dump_target.close()
                except Exception:  # noqa: BLE001
                    pass

        # Fallback: faulthandler direct. Writes from the C signal handler, so it
        # still works on a thread blocked inside a C call running no bytecode —
        # which is precisely the case being investigated.
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
