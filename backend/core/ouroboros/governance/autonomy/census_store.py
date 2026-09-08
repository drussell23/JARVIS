"""The census as an eventual-consistency store, never a blocking call.

The TestWatcher census runs the repository's test suite in pytest subprocesses.
On this tree that is minutes, and the subprocesses do not cooperate with the
event loop — which is why ``asyncio.wait_for`` around it does not save you.
``wait_for`` can only cancel at an ``await`` boundary; synchronous work inside
a coroutine, or a thread the coroutine is blocked on, runs to completion no
matter what the timeout says. A budget that cannot be enforced is not a budget,
and the Sentinel's first pass sat behind exactly that.

So the census stops being something the critical path *calls*. It becomes
something the critical path *reads*:

* :meth:`CensusStore.snapshot` returns instantly — the last completed census,
  or ``None``. It never starts work, never awaits, and never blocks.
* :meth:`CensusStore.ensure_refreshing` starts a background refresh when the
  data is stale and none is already running, then returns immediately.

The Sentinel therefore ticks at the speed of a filesystem walk, and the census
improves the ranking of *later* passes as it lands. Strong evidence when it is
available, weaker evidence when it is not, and never a stall waiting for the
difference.

## Eventual consistency means the LAST good answer survives

A failed or timed-out refresh leaves the previous snapshot in place. The
alternative — clearing on failure — would mean a single flaky run downgrades
the organism's evidence to nothing, which is worse than slightly stale truth.
Staleness is visible (:attr:`CensusSnapshot.age_s`) so a consumer can decide;
absence is not something a consumer can reason about at all.

## Single-flight, bounded, and thread-isolated

One refresh at a time — a second would double the subprocess load on a machine
already running the organism. The whole refresh executes via
``asyncio.to_thread`` so the main loop's heartbeat is never the thing waiting,
and it carries a hard deadline after which the result is DISCARDED rather than
awaited. The thread may still be finishing underneath (Python cannot kill a
thread), which is precisely why the deadline is enforced on the *waiter* and
the abandoned result is dropped on arrival instead of being written late over
fresher data.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.CensusStore")

__all__ = [
    "CensusSnapshot",
    "CensusStore",
    "census_max_age_s",
    "census_refresh_budget_s",
    "get_default_store",
    "reset_default_store",
]

_ENV_MAX_AGE = "JARVIS_CENSUS_MAX_AGE_S"
_ENV_REFRESH_BUDGET = "JARVIS_CENSUS_REFRESH_BUDGET_S"
_ENV_REFRESH_ENABLED = "JARVIS_CENSUS_REFRESH_ENABLED"


def census_refresh_enabled() -> bool:
    """Whether the Sentinel may TRIGGER a census. Default FALSE. NEVER raises.

    Moving the census off the critical path stopped it blocking a pass, but it
    did not make it cheap: a refresh shards the suite across many concurrent
    pytest subprocesses, and on this tree that destabilised the whole session
    — a 2400s run died at 79s with six ops in flight and no shutdown sequence,
    while the census was spawning its swarm.

    So triggering one is opt-in. Reading a census someone else produced stays
    free and always on, because reads cost nothing; what is gated is the
    organism deciding, on its own, to run the entire test suite while it is
    also trying to do work. The cheap tier alone yields candidates in ~1s and
    dispatches, which is the behaviour a landing needs first.
    """
    raw = (os.environ.get(_ENV_REFRESH_ENABLED, "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def census_max_age_s() -> float:
    """Beyond this age a snapshot is STALE and a refresh is due.

    Derived from the loop's own cadence: evidence older than a few passes is
    describing a tree the organism has already changed. Not a constant —
    a fast session should re-census more often than a slow one.
    """
    explicit = _env_float(_ENV_MAX_AGE, 0.0)
    if explicit > 0:
        return explicit
    pipeline = _env_float("JARVIS_PIPELINE_TIMEOUT_S", 0.0)
    if pipeline > 0:
        return max(120.0, pipeline)
    return 900.0


def census_refresh_budget_s() -> float:
    """How long a refresh may take before its result is abandoned."""
    explicit = _env_float(_ENV_REFRESH_BUDGET, 0.0)
    if explicit > 0:
        return explicit
    pipeline = _env_float("JARVIS_PIPELINE_TIMEOUT_S", 0.0)
    if pipeline > 0:
        return max(60.0, pipeline / 2.0)
    return 600.0


@dataclass(frozen=True)
class CensusSnapshot:
    """One completed census, with the age a consumer needs to judge it."""

    failures: Tuple[Any, ...]
    taken_at: float
    duration_s: float = 0.0
    partial: bool = False

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.taken_at)

    def is_fresh(self, max_age_s: Optional[float] = None) -> bool:
        limit = census_max_age_s() if max_age_s is None else max_age_s
        return self.age_s <= limit

    def render(self) -> str:
        tag = " (partial)" if self.partial else ""
        return (
            f"{len(self.failures)} red{tag}, {self.age_s:.0f}s old, "
            f"took {self.duration_s:.0f}s"
        )


class CensusStore:
    """Non-blocking access to the most recent census. NEVER raises."""

    def __init__(self) -> None:
        self._snapshot: Optional[CensusSnapshot] = None
        self._task: Optional[asyncio.Task] = None
        self._last_attempt_at: float = 0.0
        self._failures: int = 0

    # -- the read path: instant, always ----------------------------------

    def snapshot(self, *, max_age_s: Optional[float] = None) -> Optional[CensusSnapshot]:
        """The last completed census if it is fresh enough, else None.

        Never starts work and never awaits. A consumer on the critical path
        can call this as freely as reading a variable, which is the whole
        point.
        """
        snap = self._snapshot
        if snap is None:
            return None
        return snap if snap.is_fresh(max_age_s) else None

    def stale_snapshot(self) -> Optional[CensusSnapshot]:
        """The last census regardless of age — for telemetry, not decisions."""
        return self._snapshot

    @property
    def refreshing(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- the write path: background, single-flight, bounded ---------------

    def ensure_refreshing(self, watcher: Any) -> bool:
        """Start a refresh if one is due and none is running. Returns whether
        a new refresh was started. Never awaits the census."""
        if watcher is None or self.refreshing:
            return False
        if not census_refresh_enabled():
            # Reads stay free; TRIGGERING the suite is the expensive act.
            return False
        snap = self._snapshot
        if snap is not None and snap.is_fresh():
            return False
        # Back off after repeated failure so a broken suite is not re-run
        # every pass; the same geometric shape the target cooldown uses.
        if self._failures:
            wait = min(census_refresh_budget_s() * (2 ** min(self._failures - 1, 5)),
                       census_max_age_s() * 4)
            if (time.time() - self._last_attempt_at) < wait:
                return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False          # no loop -> nothing to schedule onto
        self._last_attempt_at = time.time()
        self._task = loop.create_task(
            self._refresh(watcher), name="census_refresh",
        )
        logger.info("[CensusStore] refresh started (background)")
        return True

    async def _refresh(self, watcher: Any) -> None:
        """Run the census OFF the event loop, bounded. NEVER raises."""
        started = time.monotonic()
        budget = census_refresh_budget_s()
        try:
            failures = await asyncio.wait_for(
                asyncio.to_thread(self._run_census_blocking, watcher),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            # The thread may still be running underneath — Python cannot kill
            # one. That is exactly why the deadline is enforced HERE, on the
            # waiter, and the late result is dropped rather than written over
            # fresher data when it eventually arrives.
            self._failures += 1
            logger.warning(
                "[CensusStore] CensusSubprocessTimeout after %.0fs — result "
                "abandoned; the previous snapshot (%s) stands, and discovery "
                "keeps ticking on cheap signals",
                budget,
                self._snapshot.render() if self._snapshot else "none",
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            logger.warning("[CensusStore] refresh failed: %r — snapshot unchanged", exc)
            return

        if failures is None:
            self._failures += 1
            return
        self._failures = 0
        self._snapshot = CensusSnapshot(
            failures=tuple(failures),
            taken_at=time.time(),
            duration_s=time.monotonic() - started,
        )
        logger.warning("[CensusStore] census refreshed: %s", self._snapshot.render())

    @staticmethod
    def _run_census_blocking(watcher: Any) -> Optional[Sequence[Any]]:
        """Drive the watcher's census from a WORKER THREAD.

        ``run_census`` is a coroutine, so it needs a loop — and it must not be
        this process's main one, which is the entire problem being solved. A
        private loop in this thread gives the census somewhere to run where
        its blocking subprocess work cannot starve the organism's heartbeat.
        """
        try:
            result = asyncio.run(watcher.run_census())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CensusStore] census raised in worker: %r", exc)
            return None
        try:
            failures, _passed, _skipped = result
            return list(failures or ())
        except (TypeError, ValueError):
            return list(result or ()) if result else []

    async def aclose(self) -> None:
        """Abandon any in-flight refresh. NEVER raises."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass


_DEFAULT: Optional[CensusStore] = None


def get_default_store() -> CensusStore:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = CensusStore()
    return _DEFAULT


def reset_default_store() -> None:
    """Test seam."""
    global _DEFAULT
    _DEFAULT = None
