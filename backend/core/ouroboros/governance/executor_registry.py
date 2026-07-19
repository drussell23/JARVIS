"""Process-pool executor registry — orderly drain before the reaper.

Operator paste 2026-07-18: at teardown the Preemption Shield's blind
psutil sweep SIGTERM'd EVERY child — including multiprocessing's
``resource_tracker`` janitor process. The relaunched tracker starts
with an empty registry, so every subsequent semaphore/shm unregister
prints a raw ``KeyError: '/mp-…'`` traceback to inherited stderr —
a wall the logging layer can never filter. Two-part root fix:

  1. Long-lived ``ProcessPoolExecutor`` owners register here at
     creation (weakref — the registry never extends a pool's life).
     :func:`shutdown_all` drains them gracefully (workers exit clean,
     the tracker unregisters exactly once) BEFORE any signal sweep.
  2. The shield's sweep exempts the tracker process itself (killing
     the janitor IS the spam).

Stdlib-only, no policy imports, NEVER raises anywhere.
"""
from __future__ import annotations

import logging
import weakref
from typing import Any

logger = logging.getLogger("Ouroboros.ExecutorRegistry")

_POOLS: "weakref.WeakSet[Any]" = weakref.WeakSet()


def register(executor: Any) -> None:
    """Track a long-lived executor for orderly teardown. NEVER raises."""
    try:
        _POOLS.add(executor)
    except Exception:  # noqa: BLE001
        pass


def shutdown_all() -> int:
    """Gracefully drain every registered executor (non-blocking:
    ``wait=False, cancel_futures=True`` — workers exit as soon as
    their current item ends; nothing new starts). Returns the count
    drained. NEVER raises."""
    count = 0
    for pool in list(_POOLS):
        try:
            pool.shutdown(wait=False, cancel_futures=True)
            count += 1
        except TypeError:
            # <3.9 signature safety — cancel_futures unsupported.
            try:
                pool.shutdown(wait=False)
                count += 1
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
    if count:
        logger.debug("[ExecutorRegistry] drained %d pool(s)", count)
    return count


__all__ = ["register", "shutdown_all"]
