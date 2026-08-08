"""An event-loop latency watchdog that hunts a stall instead of waiting for one.

WHAT IT REPLACES
----------------
The standing instrument for the `ov` freeze is `kill -USR1` — a human,
present at the moment of the wedge, sending a signal twice and comparing
stacks. That catches a total freeze and nothing else. The stalls that PRECEDE
one are invisible: nobody sends a signal for a 300 ms hitch, and a hitch is
where the cause is still legible.

This measures every tick, so a hitch is data rather than an impression.

THE MEASUREMENT
---------------
An in-loop task sleeps a fixed interval and records how LATE its wakeup was:

    lateness = elapsed_wall_time - requested_sleep

That number is the loop's scheduling delay, and it is the only honest measure
of "is the loop being starved" available from inside it. A busy coroutine, a
blocking call on the loop thread, GIL contention with a hot background thread,
a `run_in_terminal` suspension that outstays its welcome — all of them show up
here as lateness, whatever their cause.

WHY MEDIAN + MAD, AND NOT MEAN + STANDARD DEVIATION
---------------------------------------------------
A σ-threshold is the intuitive choice and it is self-defeating for this signal.
Latency distributions are heavy-tailed, and the samples we are hunting ARE the
tail — so every stall inflates the very standard deviation meant to catch the
next one. Under a sustained storm the threshold climbs to meet the anomaly and
the detector goes quiet exactly when it matters. A detector blinded by the
thing it hunts is not a detector.

The median absolute deviation has a 50% breakdown point: half the window can be
garbage before the estimate moves. Scaled by 1.4826 it is a consistent
estimator of σ for normally distributed data, so a threshold of "k sigmas"
still means what it says — it is simply computed in a way the outliers cannot
drag. Both figures are reported, so the difference is visible rather than
asserted.

WHY THE DUMP IS ARMED IN C AND NOT SCHEDULED IN PYTHON
------------------------------------------------------
A watchdog that shares a resource with the system it guards is not a watchdog
(the Slice 47 rule, arrived at here from a new direction). The resource an
event-loop watchdog would naturally share is the GIL — and the total-wedge case
is precisely the one where the main thread will not release it. A Python
sentinel thread cannot run a single bytecode to report that, so it would be
mute in the only situation that matters.

``faulthandler.dump_traceback_later`` runs on a C thread and writes from C. It
does not need the interpreter to be available, so it fires while the loop is
wedged, and it costs nothing while the loop is healthy.

The trick is that the timer is RE-ARMED on every healthy tick with a deadline
recomputed from live statistics. A loop that keeps ticking keeps pushing its
own execution date back and the timer never fires. A loop that stops ticking
stops postponing, and the dump lands on its own. There is no polling, no
sentinel thread, and no fixed timeout anywhere.

THE OBSERVER EFFECT
-------------------
Per tick: one `monotonic()`, one ring append, one median/MAD over a bounded
window, and two C calls to cancel/re-arm the timer. At the default interval
that is a few microseconds of work per second. The 29 kHz output ceiling
measured on this surface is four orders of magnitude away from being disturbed
by it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Deque, Optional, Tuple

from collections import deque

logger = logging.getLogger("Ouroboros.LoopWatchdog")

#: Makes MAD a consistent estimator of σ for normally distributed data. A
#: mathematical constant of the estimator, not a tunable: changing it would not
#: adjust sensitivity, it would make the reported "sigmas" mean something else.
_MAD_TO_SIGMA = 1.4826


def _env_float(name: str, default: float) -> float:
    try:
        raw = str(os.environ.get(name, "")).strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        logger.debug("[LoopWatchdog] ignoring malformed %s", name)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.environ.get(name, "")).strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        logger.debug("[LoopWatchdog] ignoring malformed %s", name)
        return default


def watchdog_enabled() -> bool:
    """Default ON. It is a few microseconds a second and it is the only thing
    that can see a stall nobody was watching for."""
    raw = str(os.environ.get("JARVIS_LOOP_WATCHDOG_ENABLED", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


class LatencyWindow:
    """A bounded rolling window with robust dispersion.

    Deliberately not a running mean: an anomaly must not permanently move the
    baseline, and a window that forgets is what lets the detector re-arm after
    a storm passes.
    """

    def __init__(self, size: int) -> None:
        self._samples: Deque[float] = deque(maxlen=max(int(size), 1))

    def __len__(self) -> int:
        return len(self._samples)

    def add(self, value: float) -> None:
        self._samples.append(float(value))

    @property
    def samples(self) -> Tuple[float, ...]:
        return tuple(self._samples)

    def median(self) -> float:
        n = len(self._samples)
        if n == 0:
            return 0.0
        ordered = sorted(self._samples)
        mid = n // 2
        if n % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def mad_sigma(self) -> float:
        """Dispersion as a σ-equivalent, resistant to the tail we are hunting."""
        n = len(self._samples)
        if n == 0:
            return 0.0
        med = self.median()
        deviations = sorted(abs(s - med) for s in self._samples)
        mid = n // 2
        if n % 2:
            mad = deviations[mid]
        else:
            mad = (deviations[mid - 1] + deviations[mid]) / 2.0
        return mad * _MAD_TO_SIGMA

    def mean(self) -> float:
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)

    def stdev(self) -> float:
        """Reported alongside MAD so the contamination is visible, never used
        as the trigger."""
        n = len(self._samples)
        if n < 2:
            return 0.0
        mu = self.mean()
        var = sum((s - mu) ** 2 for s in self._samples) / (n - 1)
        return var ** 0.5


class LoopLatencyWatchdog:
    """Measures loop scheduling delay; arms a C-level dump against a wedge."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        window: Optional[int] = None,
        sigma: Optional[float] = None,
        min_samples: Optional[int] = None,
        interval_floor_multiple: Optional[float] = None,
        dump_file: Any = None,
        on_anomaly: Optional[Callable[[dict], None]] = None,
    ) -> None:
        # Sampling period. Short enough that a sub-second hitch is caught,
        # long enough that the task is invisible in a profile.
        self.interval_s = interval_s if interval_s is not None else _env_float(
            "JARVIS_LOOP_WATCHDOG_INTERVAL_S", 0.25)
        # How much history defines "normal". Bounded so an anomaly ages out.
        self.window_size = window if window is not None else _env_int(
            "JARVIS_LOOP_WATCHDOG_WINDOW", 120)
        # Sensitivity, in σ-equivalents above the median.
        self.sigma = sigma if sigma is not None else _env_float(
            "JARVIS_LOOP_WATCHDOG_SIGMA", 6.0)
        # No verdict before there is a baseline to have a verdict about.
        self.min_samples = min_samples if min_samples is not None else _env_int(
            "JARVIS_LOOP_WATCHDOG_MIN_SAMPLES", 24)
        # The degenerate case that would otherwise make this useless: on a
        # perfectly regular loop MAD collapses to ~0, every sub-millisecond
        # jitter becomes "infinite sigmas", and the log fills with nothing.
        #
        # The floor is a MULTIPLE OF THE SAMPLING INTERVAL, and the default of
        # 1.0 is a statement about resolution rather than a tuned constant: a
        # sampler cannot resolve a stall shorter than its own period, so a
        # lateness below one interval is indistinguishable from ordinary
        # scheduling jitter no matter what the statistics say about it.
        #
        # An earlier 0.5 default proved the point by failing — at a 20 ms
        # interval it tolerated only 10 ms, and a perfectly healthy loop on a
        # loaded machine tripped it with a 12.8 ms hiccup. A detector that
        # fires on ordinary jitter gets muted, and a muted detector is the
        # state this replaces.
        self.interval_floor_multiple = (
            interval_floor_multiple if interval_floor_multiple is not None
            else _env_float("JARVIS_LOOP_WATCHDOG_FLOOR_MULTIPLE", 1.0))

        self._window = LatencyWindow(self.window_size)
        self._task: Optional[asyncio.Task] = None
        self._dump_file = dump_file
        self._on_anomaly = on_anomaly
        self._armed = False
        self._atexit_registered = False

        self.anomalies = 0
        self.ticks = 0
        self.worst_lateness = 0.0
        self.last_threshold = 0.0

    # -- statistics ---------------------------------------------------------
    def threshold(self) -> float:
        """The lateness above which this tick is anomalous.

        Two conditions, and a sample must clear BOTH: statistically unusual
        against the rolling baseline, AND large enough in absolute terms to
        matter at the configured resolution.
        """
        statistical = self._window.median() + self.sigma * self._window.mad_sigma()
        floor = self.interval_s * self.interval_floor_multiple
        return max(statistical, floor)

    def snapshot(self) -> dict:
        """Everything a reader needs to judge the verdict for themselves."""
        return {
            "ticks": self.ticks,
            "samples": len(self._window),
            "median_s": round(self._window.median(), 6),
            "mad_sigma_s": round(self._window.mad_sigma(), 6),
            # Reported for contrast: on a contaminated window this is visibly
            # larger than the MAD estimate, which is the argument for using MAD
            # made as data rather than as prose.
            "mean_s": round(self._window.mean(), 6),
            "stdev_s": round(self._window.stdev(), 6),
            "threshold_s": round(self.last_threshold, 6),
            "worst_lateness_s": round(self.worst_lateness, 6),
            "anomalies": self.anomalies,
        }

    # -- the C-level deadline ----------------------------------------------
    def _rearm(self, deadline_s: float) -> None:
        """Push the dump timer out to ``deadline_s`` from now.

        Cancel-then-set, every healthy tick. The loop postpones its own autopsy
        for as long as it keeps running; the moment it stops, the postponement
        stops with it and the dump fires from C.
        """
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(
                max(deadline_s, self.interval_s),
                repeat=False,
                file=self._dump_file,
                exit=False,          # diagnose a wedge, never kill it
            )
            self._armed = True
        except Exception:  # noqa: BLE001 — diagnostics must never be fatal
            logger.debug("[LoopWatchdog] could not arm the dump timer",
                         exc_info=True)

    def _disarm(self) -> None:
        if not self._armed:
            return
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
        except Exception:  # noqa: BLE001
            pass
        self._armed = False

    # -- the sampler --------------------------------------------------------
    def observe(self, lateness: float) -> Optional[dict]:
        """Feed one measurement. Returns the anomaly record, or None.

        Separated from the sleeping loop so the decision is testable without
        real time passing — a detector whose logic can only be exercised by
        waiting is a detector nobody exercises.
        """
        self.ticks += 1
        lateness = max(float(lateness), 0.0)
        self.worst_lateness = max(self.worst_lateness, lateness)

        # Warm-up: record, never judge. A verdict from four samples is noise
        # with a decimal point.
        if len(self._window) < self.min_samples:
            self._window.add(lateness)
            self.last_threshold = self.threshold()
            return None

        limit = self.threshold()
        self.last_threshold = limit
        anomalous = lateness > limit

        # The anomaly is EXCLUDED from the baseline it was judged against.
        # Admitting it would let a sustained stall teach the detector that
        # stalling is normal -- the masking failure this design exists to
        # avoid, arriving by the back door.
        if not anomalous:
            self._window.add(lateness)
            return None

        self.anomalies += 1
        record = {
            "lateness_s": round(lateness, 6),
            "sigmas": round(
                (lateness - self._window.median())
                / self._window.mad_sigma(), 2,
            ) if self._window.mad_sigma() > 0 else None,
            **self.snapshot(),
        }
        logger.warning(
            "[LoopWatchdog] event loop stalled %.3fs (threshold %.3fs, "
            "median %.4fs, mad-sigma %.4fs, stdev %.4fs) — %d/%d ticks",
            lateness, limit, self._window.median(), self._window.mad_sigma(),
            self._window.stdev(), self.anomalies, self.ticks,
        )
        if self._on_anomaly is not None:
            try:
                self._on_anomaly(record)
            except Exception:  # noqa: BLE001
                logger.debug("[LoopWatchdog] anomaly sink raised",
                             exc_info=True)
        return record

    async def _run(self) -> None:
        # perf_counter, not time(): a wall clock that steps backwards during an
        # NTP correction would manufacture a stall that never happened.
        while True:
            began = time.perf_counter()
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                raise
            elapsed = time.perf_counter() - began
            self.observe(elapsed - self.interval_s)
            # Re-arm AFTER observing, with a deadline derived from the
            # baseline this tick just updated.
            self._rearm(self.interval_s + self.threshold())

    # -- lifecycle ----------------------------------------------------------
    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
        """Begin sampling. Idempotent; NEVER raises."""
        if self._task is not None and not self._task.done():
            return True
        try:
            loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[LoopWatchdog] no running loop; not started")
            return False
        try:
            self._task = loop.create_task(self._run())
        except Exception:  # noqa: BLE001
            logger.debug("[LoopWatchdog] could not start", exc_info=True)
            return False

        # Disarm at exit, whatever route the process takes out.
        #
        # The sampling task is harmless to leak — the interpreter is going away
        # regardless. The C-level dump deadline is NOT: it is scheduled against
        # a real timer and would fire during shutdown, writing a full
        # multi-thread stack dump for a process that is merely finishing
        # normally. An operator reading that log would find an autopsy of a
        # healthy exit, which is exactly the kind of false evidence this whole
        # instrument exists to stop producing.
        #
        # Registered here rather than at the call site because every caller
        # would otherwise have to remember, and the one that forgot would be
        # discovered by someone reading a crash log that describes nothing.
        if not self._atexit_registered:
            try:
                import atexit
                atexit.register(self._disarm)
                self._atexit_registered = True
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            "[LoopWatchdog] armed — %.0fms sampling, %d-sample window, "
            "%.1f sigma, dump deadline recomputed per tick",
            self.interval_s * 1000.0, self.window_size, self.sigma,
        )
        return True

    def stop(self) -> None:
        """Stop sampling and cancel the pending dump. NEVER raises."""
        self._disarm()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()


def install_loop_watchdog(**kwargs: Any) -> Optional[LoopLatencyWatchdog]:
    """Arm the watchdog on the running loop, writing to the shared crash log.

    Returns the instance, or None if disabled or unstartable. The dump target
    is the SAME descriptor the SIGUSR1 trap uses, so an operator reads one file
    whether the stacks were requested by hand or produced automatically.
    """
    if not watchdog_enabled():
        return None
    dump_file = None
    try:
        from backend.core.ouroboros.battle_test.oob_diagnostics import (
            crash_log_handle,
        )
        dump_file = crash_log_handle()
    except Exception:  # noqa: BLE001
        dump_file = None      # faulthandler falls back to stderr

    watchdog = LoopLatencyWatchdog(dump_file=dump_file, **kwargs)
    if not watchdog.start():
        return None
    return watchdog
