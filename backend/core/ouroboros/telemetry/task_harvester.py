"""Nothing dies quietly.

WHAT THIS EXISTS TO STOP
--------------------------
Every boot logged this, and it was the whole message::

    [HUD-Gov] GovernedLoopService failed:
    [HUD] Ouroboros governance DEGRADED — partial pipeline

Nothing after the colon. The cause turned out to be two defects sharing one
line in `hud_governance_boot`::

    await asyncio.wait_for(asyncio.shield(gls.start()), timeout=30.0)
    ...
    logger.warning("[HUD-Gov] GovernedLoopService failed: %s", exc)

1. ``str(asyncio.TimeoutError())`` is the EMPTY STRING. Formatting an
   exception with ``%s`` prints its message, and that class has none — so a
   thirty-second timeout rendered as silence. Any exception can do this;
   `KeyError` prints a bare key, `StopIteration` prints nothing.

2. ``asyncio.shield`` means the timeout abandons the WAIT, not the WORK.
   `gls.start()` carried on running with nobody awaiting it. If it later
   raised, that exception went to the loop's default handler and the
   traceback was lost; if it later SUCCEEDED, governance was running while
   the system had already declared itself DEGRADED. Both are worse than a
   crash, because a crash tells you something.

An un-awaited task is a black hole with a schedule. This makes one impossible
to open silently: attach a done-callback, extract the FULL traceback, log it
at a level somebody reads, and route it to the organism that can act on it.

WHY IT BUFFERS
----------------
The failure this was written for happens DURING boot — which is exactly when
`RuntimeHealthSensor` may not exist yet, because the intake layer is still
coming up. A harvester that simply looked the sensor up would drop the one
traceback it was built to catch.

So findings are held in a small ring until a sensor registers, then flushed.
Bounded, because a boot that fails a hundred times has one problem, not a
hundred, and the first few tell you which.

DRY
-----
No logging framework and no new envelope. It formats with `traceback`, logs
through `logging`, and hands the finding to `RuntimeHealthSensor.report`,
which emits through the SAME `make_envelope` + `router.ingest` path a polled
finding uses. The only thing added is the entry point.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.TaskHarvester")

TASK_HARVESTER_SCHEMA_VERSION: str = "task_harvester.v1"


def harvester_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off means background tasks die the way they did before — silently. There
    is no reason to want that, so this is a rollback switch rather than a
    posture.
    """
    return (os.environ.get("JARVIS_TASK_HARVESTER_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def _buffer_size() -> int:
    try:
        raw = (os.environ.get("JARVIS_TASK_HARVESTER_BUFFER", "") or "").strip()
        return max(1, min(200, int(raw))) if raw else 25
    except (TypeError, ValueError):
        return 25


def describe_exception(exc: BaseException) -> str:
    """A one-line description that is NEVER empty. NEVER raises.

    `%s` on an exception prints its message, and plenty of exception classes
    have none — `asyncio.TimeoutError` is the one that cost us this bug, but
    `StopIteration`, `CancelledError` and any bare `raise SomeError` behave
    identically. Leading with the TYPE means a nameless exception still says
    what it was.
    """
    try:
        name = type(exc).__name__
        message = str(exc).strip()
        return f"{name}: {message}" if message else name
    except Exception:  # noqa: BLE001
        return "unprintable exception"


def format_traceback(exc: BaseException, limit: int = 40) -> str:
    """The full traceback as text. NEVER raises."""
    try:
        return "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__, limit=limit)).strip()
    except Exception:  # noqa: BLE001
        return describe_exception(exc)


@dataclass
class TaskFailure:
    """One background task that died, and everything known about it."""

    what: str
    summary: str
    traceback_text: str = ""
    severity: str = "high"
    at: float = field(default_factory=time.time)
    target_files: tuple = ()

    def as_finding(self) -> Any:
        """A `HealthFinding` for the runtime-health sensor. NEVER raises."""
        from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
            HealthFinding,
        )
        return HealthFinding(
            category="background_task_failure",
            severity=self.severity,
            summary=f"{self.what} failed: {self.summary}",
            details={
                "what": self.what,
                "traceback": self.traceback_text[:4000],
                "detected_at": self.at,
                "schema_version": TASK_HARVESTER_SCHEMA_VERSION,
            },
            target_files=self.target_files or ("backend/main.py",),
        )


class TaskHarvester:
    """Collects what background tasks take with them. NEVER raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: List[TaskFailure] = []
        self._harvested = 0
        self._routed = 0
        self._watched = 0

    # -- the entry point -------------------------------------------------

    def watch(self, task: Any, *, what: str,
              target_files: tuple = ()) -> Any:
        """Make *task* unable to fail silently. Returns it. NEVER raises.

        Wraps rather than replaces: the caller keeps the task and may still
        await it. A task that is awaited AND watched reports once, because
        `add_done_callback` fires exactly once per task.
        """
        try:
            if not harvester_enabled() or task is None:
                return task
            self._watched += 1
            task.add_done_callback(
                lambda t: self._on_done(t, what, target_files))
        except Exception:  # noqa: BLE001 — never break the thing being watched
            logger.debug("[TaskHarvester] could not watch '%s'", what,
                         exc_info=True)
        return task

    def _on_done(self, task: Any, what: str, target_files: tuple) -> None:
        try:
            if task.cancelled():
                # Cancellation is a decision somebody made, not a failure.
                logger.info("[TaskHarvester] '%s' was cancelled", what)
                return
            exc = task.exception()
            if exc is None:
                logger.info("[TaskHarvester] '%s' completed", what)
                return
            self.record(exc, what=what, target_files=target_files)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("[TaskHarvester] done-callback degraded", exc_info=True)

    def record(self, exc: BaseException, *, what: str,
               target_files: tuple = ()) -> TaskFailure:
        """Log a failure fully and route it. NEVER raises.

        Public so a caller that already caught an exception can hand it over
        without re-raising into a task — `hud_governance_boot` does exactly
        that with its timeout.
        """
        failure = TaskFailure(
            what=what, summary=describe_exception(exc),
            traceback_text=format_traceback(exc), target_files=target_files)
        try:
            self._harvested += 1
            # ERROR with the traceback attached. The whole defect was a
            # WARNING that printed nothing; a level nobody reads and a message
            # with no content are the same bug wearing different clothes.
            logger.error("[TaskHarvester] %s failed: %s\n%s",
                         what, failure.summary, failure.traceback_text)
            self._route(failure)
        except Exception:  # noqa: BLE001
            pass
        return failure

    # -- routing ---------------------------------------------------------

    def _route(self, failure: TaskFailure) -> None:
        """Hand the failure to O+V, or hold it until O+V exists. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
                get_runtime_health_sensor,
            )
            sensor = get_runtime_health_sensor()
        except Exception:  # noqa: BLE001
            sensor = None
        if sensor is None:
            with self._lock:
                self._pending.append(failure)
                del self._pending[:-_buffer_size()]
            logger.info("[TaskHarvester] no intake sensor yet — holding '%s' "
                        "(%d pending)", failure.what, len(self._pending))
            return
        self._deliver(sensor, failure)

    def _deliver(self, sensor: Any, failure: TaskFailure) -> None:
        try:
            report = getattr(sensor, "report", None)
            if report is None:
                return
            result = report(failure.as_finding())
            if asyncio.iscoroutine(result):
                # `report` is async and this may be a done-callback on a
                # closing loop. Scheduling is best-effort by design: the
                # traceback is already in the log, so a failure to route
                # costs O+V an opportunity, never the evidence.
                try:
                    asyncio.get_event_loop().create_task(result)
                except RuntimeError:
                    result.close()
                    return
            self._routed += 1
            logger.info("[TaskHarvester] routed '%s' to O+V intake",
                        failure.what)
        except Exception:  # noqa: BLE001
            logger.debug("[TaskHarvester] routing degraded", exc_info=True)

    def flush(self) -> int:
        """Deliver everything held while the sensor was absent. NEVER raises.

        Called when a sensor registers. This is the whole reason for the
        buffer: the failure worth catching happens during boot, and during
        boot the intake layer may not exist yet.
        """
        try:
            from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
                get_runtime_health_sensor,
            )
            sensor = get_runtime_health_sensor()
            if sensor is None:
                return 0
            with self._lock:
                held, self._pending = list(self._pending), []
            for f in held:
                self._deliver(sensor, f)
            if held:
                logger.info("[TaskHarvester] flushed %d held failure(s) into "
                            "O+V intake", len(held))
            return len(held)
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            pending = len(self._pending)
        return {
            "schema_version": TASK_HARVESTER_SCHEMA_VERSION,
            "enabled": harvester_enabled(),
            "watched": self._watched,
            "harvested": self._harvested,
            "routed": self._routed,
            "pending": pending,
        }


_HARVESTER: Optional[TaskHarvester] = None


def get_task_harvester() -> TaskHarvester:
    """Process-wide harvester. NEVER raises."""
    global _HARVESTER
    if _HARVESTER is None:
        _HARVESTER = TaskHarvester()
    return _HARVESTER


def watch(task: Any, *, what: str, target_files: tuple = ()) -> Any:
    """Convenience: make one task unable to fail silently. NEVER raises."""
    return get_task_harvester().watch(task, what=what,
                                      target_files=target_files)


def reset_task_harvester() -> None:
    """Testing seam. NEVER raises."""
    global _HARVESTER
    _HARVESTER = None
