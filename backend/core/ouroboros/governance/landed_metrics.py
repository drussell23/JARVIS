"""Landed work — the one number, counted where landings already pass.

## Why this is the scoreboard

Two days of soaks were read by grepping ``debug.log`` after the fact, and for
most of that time the pipeline was busy in every sense the cockpit could show —
phases advancing, tokens streaming, gates firing — while landing nothing. A
system can look alive at every layer and be inert at the only one that matters.
So the last tier is given a number of its own, on screen, all the time:
test-validated changes landed, and per soak hour.

## Where it is counted

Every engine that applies a change announces it the same way: a ``DECISION``
with ``outcome="applied"`` on the CommProtocol, emitted AFTER post-apply VERIFY
(a verify failure rolls back and never emits it). Observing that one stream
counts a landing no matter which engine produced it, including ones written
after this module. Nothing is added to any engine.

## The rate does not extrapolate

    per_hour = landed / max(1.0, uptime_hours)

The naive ``landed / uptime_hours`` divides by zero at boot and, worse, lies
loudly just after it: one landing twenty seconds in is "180 per hour". A warm-up
threshold would fix that with a tuning constant. The floor instead uses the
metric's own UNIT: until a full hour has been observed the figure is simply the
count so far, expressed per hour — it can only ever under-promise, converges on
the true rate at the hour mark, and needs no special case. ``settled`` says
which regime a reading is in, for anyone who wants to show it.

## Nothing here can hold the pipeline

``send`` is awaited by the CommProtocol for every message, so it does O(1)
in-memory work and returns. The reachability record — one ``O_APPEND`` write —
is handed to a background task. Readers (the status line ticks every ~500 ms)
take a lock for a few field reads and never touch disk. NEVER raises.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: The capability name under which landings are filed in the reachability
#: ledger: the pipeline's own final tier. REGISTERED at boot, INVOKED per
#: decision seen, EFFECTIVE per landing — so "busy but inert" is visible there
#: exactly as it is for any other capability.
CAPABILITY = "pipeline.landed"

_SECONDS_PER_HOUR = 3600.0
#: How many recent landings to remember for display. Bounded: a long soak must
#: not grow a list for ever to draw a one-line status.
_RECENT = 8


def render_landed(total: int, per_hour: float, settled: bool, uptime_s: float) -> str:
    """``landed 3 · 2.7/h`` — and, before the first hour is up, the elapsed
    time instead of a rate that has not settled. The ONE formatter: the status
    line and the log line cannot come to disagree about what was landed."""
    try:
        if settled:
            return f"landed {int(total)} · {float(per_hour):.1f}/h"
        return f"landed {int(total)} · {int(max(0.0, float(uptime_s)) // 60)}m in"
    except Exception:  # noqa: BLE001
        return "landed ?"


@dataclass(frozen=True)
class LandedSnapshot:
    total: int
    uptime_s: float
    per_hour: float
    settled: bool                 # a full hour observed: the rate is a real rate
    last_landed_age_s: Optional[float]
    recent: Tuple[str, ...]

    def render(self) -> str:
        return render_landed(self.total, self.per_hour, self.settled, self.uptime_s)


class LandedMetrics:
    """Thread-safe landing counter for one process lifetime."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._total = 0
        self._last_at: Optional[float] = None
        self._seen: Set[str] = set()
        self._recent: Deque[str] = deque(maxlen=_RECENT)

    def record(self, op_id: str, summary: str = "") -> bool:
        """Count a landing. Idempotent per ``op_id``: a decision re-emitted by a
        retrying transport, or replayed at boot, is one landing, not two."""
        try:
            key = str(op_id or "")
            with self._lock:
                if key and key in self._seen:
                    return False
                if key:
                    self._seen.add(key)
                self._total += 1
                self._last_at = self._clock()
                self._recent.append(str(summary or key)[:120])
            return True
        except Exception:  # noqa: BLE001
            return False

    def snapshot(self) -> LandedSnapshot:
        try:
            now = self._clock()
            with self._lock:
                total = self._total
                last_at = self._last_at
                recent = tuple(self._recent)
            uptime = max(0.0, now - self._started)
            hours = uptime / _SECONDS_PER_HOUR
            return LandedSnapshot(
                total=total,
                uptime_s=uptime,
                per_hour=total / max(1.0, hours),
                settled=hours >= 1.0,
                last_landed_age_s=None if last_at is None else max(0.0, now - last_at),
                recent=recent,
            )
        except Exception:  # noqa: BLE001
            return LandedSnapshot(0, 0.0, 0.0, False, None, ())


_default: Optional[LandedMetrics] = None
_default_lock = threading.Lock()


def get_landed_metrics() -> LandedMetrics:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = LandedMetrics()
    return _default


def reset_for_tests() -> None:
    global _default
    with _default_lock:
        _default = None


class LandedMetricsTransport:
    """CommProtocol transport that counts landings. Emits nothing itself."""

    def __init__(
        self, metrics: Optional[LandedMetrics] = None, *, ledger: Any = None,
    ) -> None:
        self._metrics = metrics
        self._ledger = ledger
        self._registered = False
        self._background: Set["asyncio.Task[Any]"] = set()

    def _m(self) -> LandedMetrics:
        return self._metrics if self._metrics is not None else get_landed_metrics()

    def _file(self, coro_factory: Callable[[Any], Any]) -> None:
        """Hand a ledger write to the background. The pipeline awaits ``send``;
        it must not also await a disk write made on its behalf."""
        try:
            ledger = self._ledger
            if ledger is None:
                from backend.core.ouroboros.governance.reachability_ledger import (  # noqa: PLC0415
                    default_ledger,
                )
                ledger = default_ledger()
            task = asyncio.get_running_loop().create_task(coro_factory(ledger))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        except Exception:  # noqa: BLE001
            logger.debug("[Landed] reachability record degraded", exc_info=True)

    async def send(self, msg: Any) -> None:
        try:
            kind = getattr(getattr(msg, "msg_type", None), "value", "")
            if kind != "DECISION":
                return
            if not self._registered:
                self._registered = True
                self._file(lambda led: led.registered(CAPABILITY, detail="landing observer attached"))
            payload = getattr(msg, "payload", None) or {}
            op_id = str(getattr(msg, "op_id", "") or "")
            outcome = str(payload.get("outcome") or "")
            self._file(lambda led: led.invoked(CAPABILITY, op_id=op_id, detail=outcome))
            if outcome != "applied":
                return
            summary = str(payload.get("diff_summary") or payload.get("reason_code") or "")
            if self._m().record(op_id, summary):
                snap = self._m().snapshot()
                logger.warning(
                    "[Landed] #%d op=%s — %s (%s)",
                    snap.total, op_id[:24], summary[:100] or "applied", snap.render(),
                )
                self._file(lambda led: led.effective(CAPABILITY, op_id=op_id, detail=summary[:200]))
        except Exception:  # noqa: BLE001 — a scoreboard never stops the game
            logger.debug("[Landed] transport degraded", exc_info=True)


__all__ = [
    "CAPABILITY",
    "LandedMetrics",
    "LandedMetricsTransport",
    "LandedSnapshot",
    "get_landed_metrics",
    "render_landed",
    "reset_for_tests",
]
