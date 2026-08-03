"""Is the event loop actually running, or only appearing to?

WHY THIS AND NOT MORE REASONING
---------------------------------
An operator said "lock my screen" and waited. The IPC event was received at
01:40:34 and the router did not begin until 01:40:49 — fifteen seconds during
which JARVIS was, from the room, simply not there. `Application startup
complete` landed 79 seconds after boot began.

The obvious diagnosis is "heavy initialisation blocks the loop", and it may
well be right. But `await slow_thing()` does NOT starve a loop — it yields,
and a 79-second boot can be perfectly responsive throughout. Only a
SYNCHRONOUS call that never yields starves anything. Those two produce
identical-looking logs and need opposite fixes, and the difference is not
visible in a wall-clock timeline.

`telemetry/loop_sink` already measures this, but only where somebody wrapped a
call site — it can prove a suspect guilty and can never find one. What is
missing is a witness that watches the loop ITSELF.

HOW IT WORKS
--------------
Sleep for a known interval, then measure how much longer than that it actually
took. `asyncio.sleep(0.25)` returning after 3.1 seconds means the loop could
not get back to this task for 2.85 seconds, and that is starvation by
definition — no instrumentation of the culprit required, because the
measurement is of the loop rather than of any particular call.

The lag is attributed to a WINDOW rather than a function on purpose. Naming
the guilty callable requires `loop.set_debug(True)`, whose own overhead
changes the thing being measured; this says "between 01:40:34 and 01:40:49 the
loop was gone", which is exactly the fact needed to go and look.

WHAT IT COSTS
---------------
One task, one timer, and a subtraction, at 4 Hz. It is cheaper than the log
line it emits, and it only emits when something is actually wrong.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("JARVIS.LoopSentinel")

LOOP_SENTINEL_SCHEMA_VERSION: str = "loop_sentinel.v1"


def sentinel_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    On by default because the cost is a subtraction every 250ms and the thing
    it detects is invisible without it — an assistant that is not there is
    indistinguishable, in a log, from an assistant that is merely busy.
    """
    return (os.environ.get("JARVIS_LOOP_SENTINEL_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return max(lo, min(hi, float(raw))) if raw else default
    except (TypeError, ValueError):
        return default


def probe_interval_s() -> float:
    """How often to check. NEVER raises."""
    return _env_float("JARVIS_LOOP_SENTINEL_INTERVAL_S", 0.25, 0.05, 5.0)


def stall_threshold_s() -> float:
    """Lag above which the loop is considered to have stalled. NEVER raises.

    250ms is the point at which a person notices an assistant is not
    responding. Below that it is a busy machine; above it, JARVIS is absent.
    """
    return _env_float("JARVIS_LOOP_SENTINEL_STALL_S", 0.25, 0.05, 10.0)


@dataclass
class Stall:
    """One window during which the loop could not run this task."""

    started_at: float
    lag_s: float
    #: Wall-clock, so a stall can be matched against a log line by eye.
    started_wall: str = ""

    def __str__(self) -> str:
        return f"{self.started_wall} for {self.lag_s:.2f}s"


@dataclass
class LoopHealth:
    """What the sentinel has seen. Bounded."""

    probes: int = 0
    stalls: int = 0
    worst_lag_s: float = 0.0
    total_stalled_s: float = 0.0
    recent: List[Stall] = field(default_factory=list)
    started_at: float = 0.0

    @property
    def availability(self) -> float:
        """Fraction of wall-clock the loop was responsive. NEVER raises."""
        try:
            elapsed = max(1e-6, time.monotonic() - self.started_at)
            return max(0.0, min(1.0, 1.0 - (self.total_stalled_s / elapsed)))
        except Exception:  # noqa: BLE001
            return 1.0


class LoopSentinel:
    """Watches the event loop from inside it. NEVER raises."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._health = LoopHealth()
        self._max_recent = 20

    def start(self) -> None:
        """Begin watching. Idempotent. NEVER raises.

        Started as EARLY as possible — the interesting stalls are the ones
        during boot, and a witness that arrives after the event has nothing to
        report.
        """
        try:
            if not sentinel_enabled():
                return
            if self._task is not None and not self._task.done():
                return
            self._health = LoopHealth(started_at=time.monotonic())
            self._task = asyncio.get_event_loop().create_task(self._watch())
            logger.info("[LoopSentinel] watching — will report any window "
                        "longer than %.0fms in which the loop cannot run",
                        stall_threshold_s() * 1000.0)
        except Exception:  # noqa: BLE001
            logger.debug("[LoopSentinel] start degraded", exc_info=True)

    async def stop(self) -> None:
        """Stop watching. NEVER raises."""
        try:
            t, self._task = self._task, None
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    async def _watch(self) -> None:
        interval = probe_interval_s()
        while True:
            try:
                threshold = stall_threshold_s()
                before = time.monotonic()
                await asyncio.sleep(interval)
                # Everything past the interval is time the loop owed this task
                # and could not pay. No attribution, no instrumentation — the
                # measurement IS the starvation.
                lag = (time.monotonic() - before) - interval
                self._health.probes += 1
                if lag >= threshold:
                    self._record(before, lag)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — a witness never dies of a fault
                logger.debug("[LoopSentinel] probe degraded", exc_info=True)
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return

    def _record(self, started: float, lag: float) -> None:
        try:
            h = self._health
            h.stalls += 1
            h.total_stalled_s += lag
            h.worst_lag_s = max(h.worst_lag_s, lag)
            stall = Stall(started_at=started, lag_s=lag,
                          started_wall=time.strftime("%H:%M:%S"))
            h.recent.append(stall)
            del h.recent[:-self._max_recent]
            # WARNING, not DEBUG. This is the operator's assistant being
            # absent; it belongs at the level somebody reads.
            logger.warning(
                "[LoopSentinel] EVENT LOOP STALLED %.2fs at %s — JARVIS could "
                "not respond to anything during that window (stall #%d, worst "
                "%.2fs, availability %.1f%%)",
                lag, stall.started_wall, h.stalls, h.worst_lag_s,
                h.availability * 100.0)
        except Exception:  # noqa: BLE001
            pass

    def health(self) -> Dict[str, Any]:
        """What the loop has been doing. NEVER raises."""
        h = self._health
        return {
            "schema_version": LOOP_SENTINEL_SCHEMA_VERSION,
            "enabled": sentinel_enabled(),
            "watching": self._task is not None and not self._task.done(),
            "probes": h.probes,
            "stalls": h.stalls,
            "worst_lag_s": round(h.worst_lag_s, 3),
            "total_stalled_s": round(h.total_stalled_s, 3),
            "availability": round(h.availability, 4),
            "recent": [str(s) for s in h.recent],
        }


_SENTINEL: Optional[LoopSentinel] = None


def get_loop_sentinel() -> LoopSentinel:
    """Process-wide sentinel. NEVER raises."""
    global _SENTINEL
    if _SENTINEL is None:
        _SENTINEL = LoopSentinel()
    return _SENTINEL


def reset_loop_sentinel() -> None:
    """Testing seam. NEVER raises."""
    global _SENTINEL
    _SENTINEL = None
