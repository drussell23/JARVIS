"""The cancellation phase this loop's teardown was missing.

WHAT WENT WRONG
---------------
Session ``bt-2026-08-18-021438``, at ``post_asyncio_teardown``, immediately
after "Shutdown complete"::

    [PANIC] RuntimeError: an error occurred during closing of asynchronous
            generator <async_generator object StreamEventBroker.stream_iter>
    RuntimeError: aclose(): asynchronous generator is already running
    ... ExecutionGraphProgressTracker._drain_subscriber ... same
    [PANIC] UnknownError: Task was destroyed but it is pending!

Three symptoms, one cause. ``scripts/ouroboros_battle_test.py`` owns its loop
by hand and tears it down as::

    loop.run_until_complete(harness.run())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()

:func:`asyncio.run` performs FOUR steps in that position, and this performs
the last three. The missing first one is *cancel every remaining task and let
it finish*. Without it, any task the harness did not explicitly own is still
alive and still parked inside an async generator when ``shutdown_asyncgens()``
calls ``aclose()`` on it -- which is exactly what "aclose(): asynchronous
generator is already running" means. The generators are not at fault: both
handle ``CancelledError`` and unsubscribe in ``finally``. Nobody cancelled
them. "Task was destroyed but it is pending" two seconds later is the same
un-cancelled task meeting ``loop.close()``.

WHY NOT AsyncExitStack
----------------------
An exit stack manages resources a single owner holds. Seven-plus modules
consume ``stream_iter`` from independently-spawned tasks; there is no single
owner to hold a stack, and adding one per consumer would be seven partial
fixes for a defect that lives in one place. The lifecycle phase is the defect,
so the lifecycle phase is the fix -- and it covers every future consumer
without any of them knowing this module exists.

WHY NOT asyncio.runners._cancel_all_tasks
-----------------------------------------
It is the right algorithm and the wrong contract: it ``gather``s the cancelled
tasks with no timeout, so a single task that swallows ``CancelledError`` hangs
teardown forever. This process already learned that lesson -- the
``BoundedShutdownWatchdog`` armed one frame earlier exists because a previous
session sat 1h50m in executor shutdown. Everything on this path must be
bounded, so the cancellation phase is bounded too, and reports by name whatever
outlived its deadline instead of pretending it finished.

DISCIPLINE
----------
* **Bounded** -- deadline from the environment, never a literal.
* **Adaptive sweeps** -- a cancelled task's ``finally`` may legitimately spawn
  cleanup work, so the sweep repeats until the task set is empty or the budget
  is spent. One pass would leave exactly the tasks that clean up after
  themselves.
* **No silent caps** -- survivors are named in the report. A teardown that
  quietly gave up would be the "receipt for work it did not do" pattern this
  codebase spends its time removing.
* **Never raises.** It runs inside a ``finally`` that is the last thing
  standing between a session and its artifacts.
* **Not a watchdog.** It reads no application state-ledger and makes no
  liveness judgement; it cancels, waits a bounded time, and reports. The
  Slice-47 Watchdog Isolation Invariant is untouched -- the real watchdog is
  armed independently, one frame above, and neither consults the other.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set

logger = logging.getLogger("Ouroboros.LoopTeardown")

_TRUTHY = ("1", "true", "yes", "on")

ENABLED_ENV = "JARVIS_TEARDOWN_CANCEL_ENABLED"
DEADLINE_ENV = "JARVIS_TEARDOWN_CANCEL_DEADLINE_S"
MAX_SWEEPS_ENV = "JARVIS_TEARDOWN_CANCEL_MAX_SWEEPS"


def cancel_phase_enabled() -> bool:
    """Master gate. Default ON; OFF restores the exact prior teardown."""
    return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY


def cancel_deadline_s() -> float:
    """Total budget for the whole cancellation phase.

    Default 5s. Sized well inside ``JARVIS_BATTLE_SHUTDOWN_DEADLINE_S`` (30s),
    because this phase runs INSIDE that watchdog's window and must leave room
    for ``shutdown_asyncgens`` + ``shutdown_default_executor`` behind it. A
    cooperative task answers cancellation in milliseconds; anything still
    running at five seconds is not going to finish, and waiting longer only
    spends the budget the steps after this one need."""
    try:
        return max(0.0, float(os.environ.get(DEADLINE_ENV, "5.0")))
    except (TypeError, ValueError):
        return 5.0


def max_sweeps() -> int:
    """How many times to re-scan for tasks spawned during cancellation.

    Default 3. Bounded because a task that spawns a replacement from its own
    ``finally`` on every cancellation would otherwise loop forever -- and that
    is a defect to REPORT, not to chase."""
    try:
        return max(1, int(os.environ.get(MAX_SWEEPS_ENV, "3")))
    except (TypeError, ValueError):
        return 3


@dataclass
class TeardownReport:
    """What the cancellation phase actually achieved. Honest by construction."""

    cancelled: int = 0
    finished: int = 0
    sweeps: int = 0
    #: Tasks still running when the budget ran out. NAMED, because a teardown
    #: that hid them would report success for work it abandoned.
    survivors: List[str] = field(default_factory=list)
    #: Non-cancellation exceptions raised by tasks as they wound down.
    errors: List[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def clean(self) -> bool:
        return not self.survivors and not self.errors

    def render(self) -> str:
        if self.skipped:
            return "[LoopTeardown] cancellation phase disabled"
        if self.cancelled == 0:
            return "[LoopTeardown] no outstanding tasks"
        head = (
            f"[LoopTeardown] cancelled={self.cancelled} finished={self.finished} "
            f"sweeps={self.sweeps}"
        )
        if self.survivors:
            head += f" SURVIVED={len(self.survivors)} {self.survivors[:8]}"
        if self.errors:
            head += f" errors={len(self.errors)} {self.errors[:4]}"
        return head


def _describe(task: "asyncio.Task") -> str:
    """A name a human can act on. NEVER raises."""
    try:
        name = task.get_name()
    except Exception:  # noqa: BLE001
        name = "<task>"
    try:
        coro = task.get_coro()
        qual = getattr(coro, "__qualname__", None) or getattr(
            getattr(coro, "cr_code", None), "co_name", "",
        )
    except Exception:  # noqa: BLE001
        qual = ""
    return f"{name}:{qual}" if qual else str(name)


def _live_tasks(
    loop: "Optional[asyncio.AbstractEventLoop]",
    exclude: "Set[Any]",
) -> "List[asyncio.Task]":
    """Every unfinished task except this one and any explicitly excluded."""
    try:
        current = asyncio.current_task()
    except Exception:  # noqa: BLE001
        current = None
    try:
        tasks = asyncio.all_tasks(loop) if loop is not None else asyncio.all_tasks()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in tasks:
        try:
            if t is current or t in exclude or t.done():
                continue
        except Exception:  # noqa: BLE001
            continue
        out.append(t)
    return out


async def cancel_remaining_tasks(
    *,
    deadline_s: Optional[float] = None,
    exclude: "Optional[Set[Any]]" = None,
) -> TeardownReport:
    """Cancel every outstanding task and wait, bounded, for it to finish.

    THE phase that must run before ``loop.shutdown_asyncgens()``. Returns a
    report rather than raising: this executes inside the teardown ``finally``,
    where an exception would cost the session its artifacts.
    """
    report = TeardownReport()
    if not cancel_phase_enabled():
        report.skipped = True
        return report

    budget = cancel_deadline_s() if deadline_s is None else max(0.0, deadline_s)
    excluded: "Set[Any]" = set(exclude or ())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — called off-loop
        return report

    try:
        deadline = loop.time() + budget
        for _ in range(max_sweeps()):
            pending = _live_tasks(loop, excluded)
            if not pending:
                break
            report.sweeps += 1
            for task in pending:
                try:
                    task.cancel()
                    report.cancelled += 1
                except Exception:  # noqa: BLE001
                    continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, still = await asyncio.wait(pending, timeout=remaining)
            report.finished += len(done)
            # Surface non-cancellation failures. A task that died of a real
            # error while winding down is a defect, and swallowing it here
            # would make the teardown the place bugs go to disappear.
            for task in done:
                try:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        report.errors.append(f"{_describe(task)}: {exc!r}")
                except Exception:  # noqa: BLE001
                    continue
            if still:
                # Out of budget with tasks still running: stop sweeping and
                # let the report name them.
                break

        # Whatever is left after the sweeps is a survivor, named.
        for task in _live_tasks(loop, excluded):
            report.survivors.append(_describe(task))
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.debug("[LoopTeardown] cancellation phase degraded", exc_info=True)
    return report
