"""Structured lifecycle for long-lived tasks: one token, one factory, one owner.

WHY THIS EXISTS
---------------
Measured 2026-08-05: the backend could not shut down. Detection of a dead
launcher was instantaneous and the process still needed a hard `os._exit`
every single time, because twenty-plus daemon tasks were still pending when
`asyncio.Runner.close()` finally cancelled them en masse — CloudSQL cleanup,
health and leak monitors, GCP VM monitoring, DoubleWord discovery probes,
learning-DB auto-flush, distributed lock cleanup, agent pool workers, rate
orchestrator forecasting.

None of those loops was individually wrong. Every one inspected handled
`asyncio.CancelledError` correctly. **The defect was that nobody owned them.**
They were created with bare `asyncio.create_task` from twenty different
modules, no shutdown path knew they existed, and they were left to whatever
happened to run last.

The same absence of ownership showed from the other end: `FailoverLifecycle`
was observed STARTING new work while shutdown was already under way. A system
that can spawn during teardown cannot be torn down — cancel the tasks you know
about and it grows new ones. This module makes both halves impossible rather
than discouraged.

THE TWO PRIMITIVES
------------------
1. A **cancellation token** — one process-wide, thread-safe flag saying
   "teardown has begun". Thread-safe because the answer is needed from places
   that have no event loop: signal handlers, executor threads, the parent
   watch. `threading.Event` is readable from all of them without coordination.

2. A **managed task factory** — `spawn_managed_task()`. It refuses to create
   anything once the token is set, registers what it does create, and hands
   ownership to `CoordinatedShutdownManager` so a phase cancels the tasks
   belonging to it.

WHY A TOKEN AND NOT JUST CANCELLATION
--------------------------------------
Cancellation alone is correct but slow, and slow is what made the hard exit
routine. A daemon parked in `await asyncio.sleep(300)` does eventually notice
a `CancelledError` — but only after the loop schedules it, and only if nothing
else on the loop is ahead of it. Twenty of those, several with network I/O in
flight, do not unwind inside any sane grace period.

A token turns the wait itself into a race: `sleep_or_shutdown()` waits for the
delay OR the token, whichever comes first, so a daemon leaves its loop the
moment shutdown starts rather than when a cancellation reaches it. That is the
difference between a shutdown measured in seconds and one measured in
milliseconds, and it is why the migration primitive is a *sleep replacement*
rather than an instruction to catch a different exception.

Cancellation remains the backstop: the token is cooperative, so anything that
ignores it is still cancelled by its phase, and anything that ignores THAT is
still bounded by the phase timeout. Three layers, each one narrower.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger("jarvis.lifecycle")

APP_LIFECYCLE_SCHEMA_VERSION: str = "app_lifecycle.v1"

#: Total wall-clock budget for the whole shutdown, across every phase. The
#: process must be gone before the parent watch's grace expires, or the hard
#: exit does the job and the graceful path was theatre.
ENV_TOTAL_BUDGET_S: str = "JARVIS_SHUTDOWN_TOTAL_BUDGET_S"

#: Master switch for the spawn ban. The REGISTRATION half never turns off —
#: a task the manager does not know about is the original defect — but the
#: refusal can be downgraded to a warning if a subsystem turns out to need a
#: spawn during teardown and has to be fixed on its own schedule.
ENV_BLOCK_SPAWN: str = "JARVIS_LIFECYCLE_BLOCK_LATE_SPAWN"


def total_budget_s() -> float:
    """Seconds the entire shutdown may take. NEVER raises.

    Clamped, not validated: a typo in a timing knob must not be able to hand
    the system an unbounded shutdown, which is the condition being fixed.
    """
    try:
        raw = (os.environ.get(ENV_TOTAL_BUDGET_S, "") or "").strip()
        return max(0.5, float(raw)) if raw else 5.0
    except Exception:  # noqa: BLE001
        return 5.0


def _block_late_spawn() -> bool:
    return (os.environ.get(ENV_BLOCK_SPAWN, "true") or "").strip().lower() not in (
        "0", "false", "no", "off")


class ShutdownInProgress(RuntimeError):
    """Raised when something tries to start long-lived work during teardown.

    A distinct type on purpose. Callers that legitimately race shutdown — a
    request handler finishing, a retry deciding whether to try again — should
    be able to catch exactly this and stop, without swallowing unrelated
    RuntimeErrors and turning a real bug into a silent no-op.
    """


class AppLifecycle:
    """Process-wide lifecycle state. Thread-safe. NEVER raises.

    Deliberately not an `asyncio.Event`: the question "are we shutting down?"
    is asked from signal handlers, executor threads and the parent watch, none
    of which have a running loop, and an `asyncio.Event` is bound to the loop
    that created it. `threading.Event` answers from anywhere.

    Waiters that DO have a loop are served by `wait_for_shutdown`, which
    bridges the threading primitive into async without pinning it to a loop.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str = ""
        self._at: float = 0.0
        self._lock = threading.Lock()
        # One asyncio.Event per loop that has ever waited, so async waiters
        # cost a callback rather than a thread. See `_mirror`.
        self._mirrors: "Dict[int, Any]" = {}
        self._mirror_loops: "Dict[int, Any]" = {}

    @property
    def shutdown_requested(self) -> bool:
        """The token. Cheap enough to read on every loop iteration."""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def since_s(self) -> float:
        """Seconds since shutdown was requested, or 0.0 if it has not been."""
        return (time.monotonic() - self._at) if self._at else 0.0

    def request_shutdown(self, reason: str = "unspecified") -> bool:
        """Set the token. Idempotent — the FIRST reason is kept.

        The first reason is the true one; anything after it is a consequence.
        Overwriting would replace "launcher died" with "database closed",
        which is how a postmortem loses its cause.
        """
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._at = time.monotonic()
            self._event.set()
        # Wake async waiters OUTSIDE the lock: `call_soon_threadsafe` touches
        # another loop's machinery, and holding our lock across that is how a
        # shutdown path acquires two locks in an order nothing else respects.
        self._wake_mirrors()
        logger.warning("[Lifecycle] shutdown requested: %s — long-lived task "
                       "creation is now refused", reason)
        return True

    def _mirror(self) -> "asyncio.Event":
        """An `asyncio.Event` for the CURRENT loop, mirroring the token.

        WHY NOT `run_in_executor(event.wait)`
        --------------------------------------
        That was the first implementation and it does not scale. Every waiting
        daemon parks one thread from the default executor for the whole of its
        sleep, so twenty daemons sleeping sixty seconds hold twenty threads
        indefinitely — and the default pool is shared with everything else in
        the process, so the cost lands on unrelated work. Measured at the time:
        a nine-test suite took 61 seconds, almost all of it waiting for threads.

        A mirrored `asyncio.Event` costs one object per loop and one callback
        at shutdown, regardless of how many daemons are waiting.

        Per-loop rather than global because an `asyncio.Event` is bound to the
        loop that created it — the exact "bound to a different event loop"
        failure that the IPC dispatch loop was rebuilt to avoid.
        """
        loop = asyncio.get_running_loop()
        key = id(loop)
        ev = self._mirrors.get(key)
        if ev is None:
            ev = asyncio.Event()
            with self._lock:
                self._mirrors[key] = ev
                self._mirror_loops[key] = loop
            if self._event.is_set():
                ev.set()          # requested before this loop ever asked
        return ev

    async def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """Await the token from async code. Returns True if it was set.

        Costs no thread: the wait is a plain `asyncio.Event`, woken by a
        `call_soon_threadsafe` from whoever requests shutdown.
        """
        if self._event.is_set():
            return True
        try:
            ev = self._mirror()
            if timeout is None:
                await ev.wait()
                return True
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return self._event.is_set()

    def _wake_mirrors(self) -> None:
        """Set every loop's mirror from whatever thread requested shutdown.

        `call_soon_threadsafe` because the requester is frequently NOT on the
        loop — a signal handler, the parent watch's kqueue thread, an executor.
        A loop that has already closed raises, and that is not an error worth
        propagating: a closed loop has no waiters left to wake.
        """
        with self._lock:
            pairs = list(zip(self._mirrors.values(), self._mirror_loops.values()))
        for ev, loop in pairs:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:  # noqa: BLE001
                continue

    def reset(self) -> None:
        """Testing seam. Never called in production."""
        with self._lock:
            self._event.clear()
            self._reason = ""
            self._at = 0.0
            self._mirrors.clear()
            self._mirror_loops.clear()


#: The process-wide instance. A module-level singleton rather than something
#: passed around, because the twenty subsystems that need to consult it have
#: no common ancestor to receive it from.
LIFECYCLE = AppLifecycle()


def shutdown_requested() -> bool:
    """Free function for the hot path in daemon loops. NEVER raises."""
    return LIFECYCLE.shutdown_requested


async def sleep_or_shutdown(delay: float) -> bool:
    """Sleep, unless shutdown starts first. Returns True if it did.

    THE MIGRATION PRIMITIVE. A daemon written as

        while self._running:
            await asyncio.sleep(60)
            do_work()

    waits up to a full minute before it can notice anything, so cancelling it
    is the only way to stop it and the cost is however long the loop takes to
    reach a cancellation point. Rewritten as

        while not await sleep_or_shutdown(60):
            do_work()

    it leaves immediately when the token is set, and the loop condition and
    the shutdown check become the same expression — there is no window in
    which it has woken up, seen no cancellation, and started another unit of
    work that shutdown will then have to wait for.

    Implemented as a race rather than a poll: polling would trade latency
    against wakeups, and this needs neither.
    """
    if LIFECYCLE.shutdown_requested:
        return True
    if delay <= 0:
        return LIFECYCLE.shutdown_requested
    return await LIFECYCLE.wait_for_shutdown(delay)


class ManagedTaskRegistry:
    """Every long-lived task, and which shutdown phase owns it. NEVER raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_phase: Dict[int, Set[asyncio.Task]] = {}
        self._spawned = 0
        self._refused = 0

    def add(self, task: "asyncio.Task", phase: int) -> None:
        with self._lock:
            self._by_phase.setdefault(phase, set()).add(task)
            self._spawned += 1
        # Self-removing: a registry that only grows is a leak wearing the
        # costume of a lifecycle manager.
        task.add_done_callback(lambda t: self._discard(t, phase))

    def _discard(self, task: "asyncio.Task", phase: int) -> None:
        with self._lock:
            bucket = self._by_phase.get(phase)
            if bucket:
                bucket.discard(task)

    def refused(self) -> None:
        with self._lock:
            self._refused += 1

    def tasks_for(self, phase: int) -> List["asyncio.Task"]:
        with self._lock:
            return [t for t in self._by_phase.get(phase, set()) if not t.done()]

    def all_tasks(self) -> List["asyncio.Task"]:
        with self._lock:
            return [t for s in self._by_phase.values() for t in s if not t.done()]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            live = {p: len([t for t in s if not t.done()])
                    for p, s in self._by_phase.items()}
        return {"schema_version": APP_LIFECYCLE_SCHEMA_VERSION,
                "spawned": self._spawned, "refused_late_spawn": self._refused,
                "live_by_phase": live}


REGISTRY = ManagedTaskRegistry()

#: Phases that already have a cancel-hook registered with the manager, so the
#: hook is installed once per phase rather than once per task.
_HOOKED_PHASES: Set[int] = set()
_HOOK_LOCK = threading.Lock()


def _default_phase() -> int:
    """CLEANUP, resolved lazily so importing this module stays cheap.

    Daemons import this from all over the codebase; pulling in the whole
    shutdown stack — and its Trinity IPC dependencies — at import time would
    make adoption expensive and invite import cycles.
    """
    try:
        from backend.core.coordinated_shutdown import ShutdownPhase
        return int(ShutdownPhase.CLEANUP)
    except Exception:  # noqa: BLE001
        return 4


def _ensure_phase_hook(phase: int) -> None:
    """Register ONE hook per phase that cancels that phase's tasks.

    One hook per phase, not one per task: a hook per task would make the
    manager's phase timeout meaningless, since twenty hooks each allowed ten
    seconds is a two-hundred-second phase. Cancelling a phase's tasks together
    is also semantically right — tasks in the same phase have no ordering
    relationship with each other, which is what putting them in one phase says.
    """
    with _HOOK_LOCK:
        if phase in _HOOKED_PHASES:
            return
        _HOOKED_PHASES.add(phase)

    async def _cancel_phase_tasks() -> None:
        tasks = REGISTRY.tasks_for(phase)
        if not tasks:
            return
        logger.info("[Lifecycle] phase %s: cancelling %d managed task(s)",
                    phase, len(tasks))
        for t in tasks:
            t.cancel()
        # Gathered with return_exceptions so one task that raises on the way
        # out cannot prevent the others from being awaited. The phase timeout
        # above this bounds the whole thing.
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        from backend.core.coordinated_shutdown import (
            ShutdownPhase, get_shutdown_manager_sync,
        )
        mgr = get_shutdown_manager_sync()
        mgr.register_hook(
            name=f"managed_tasks_phase_{phase}",
            phase=ShutdownPhase(phase),
            callback=_cancel_phase_tasks,
            priority=10,          # before hand-written hooks in the same phase
            timeout=max(1.0, total_budget_s() / 2.0),
        )
    except Exception:  # noqa: BLE001
        # A registry that cannot reach the manager still beats a bare
        # create_task: the tasks are tracked and `cancel_all` can reach them.
        logger.debug("[Lifecycle] could not register phase hook", exc_info=True)


def spawn_managed_task(
    coro: "Coroutine[Any, Any, Any]",
    *,
    name: Optional[str] = None,
    phase: Optional[int] = None,
) -> "asyncio.Task":
    """Create a long-lived task that shutdown knows about. Replaces create_task.

    Two guarantees, and the second is the one that was missing:

    1. **Refusal during teardown.** Once the token is set this raises
       `ShutdownInProgress` and the coroutine is closed without ever running.
       This is the Hydra fix — `FailoverLifecycle` was seen starting recovery
       work *during* shutdown, and a system that grows new tasks while being
       torn down cannot be torn down.

    2. **Registration.** The task is filed under a shutdown phase, and that
       phase cancels it. Nothing has to remember to clean it up, because
       remembering is precisely what failed twenty times over.

    The coroutine is explicitly closed on refusal. Dropping it would leave an
    un-awaited coroutine to surface later as a "never awaited" warning during
    interpreter teardown — a confusing second symptom of a correct refusal.
    """
    if LIFECYCLE.shutdown_requested and _block_late_spawn():
        REGISTRY.refused()
        label = name or getattr(coro, "__qualname__", repr(coro))
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass
        raise ShutdownInProgress(
            f"refusing to spawn '{label}' — shutdown began "
            f"{LIFECYCLE.since_s:.2f}s ago (reason: {LIFECYCLE.reason}). "
            f"A system that spawns during teardown cannot be torn down.")

    resolved = _default_phase() if phase is None else int(phase)
    task = asyncio.get_event_loop().create_task(coro) if name is None \
        else asyncio.get_event_loop().create_task(coro, name=name)
    REGISTRY.add(task, resolved)
    _ensure_phase_hook(resolved)
    return task


async def cancel_all_managed(timeout: Optional[float] = None) -> int:
    """Cancel every managed task, whatever phase owns it. NEVER raises.

    The escape hatch for callers with no shutdown manager — tests, the legacy
    brainstem path, an embedded run. Returns how many were still live.
    """
    tasks = REGISTRY.all_tasks()
    if not tasks:
        return 0
    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout if timeout is not None else total_budget_s())
    except asyncio.TimeoutError:
        logger.warning("[Lifecycle] %d managed task(s) did not finish "
                       "cancelling within budget", len(tasks))
    except Exception:  # noqa: BLE001
        pass
    return len(tasks)


def stats() -> Dict[str, Any]:
    """Observability surface. NEVER raises."""
    return {
        "shutdown_requested": LIFECYCLE.shutdown_requested,
        "reason": LIFECYCLE.reason,
        "since_s": round(LIFECYCLE.since_s, 3),
        "total_budget_s": total_budget_s(),
        "block_late_spawn": _block_late_spawn(),
        **REGISTRY.stats(),
    }
