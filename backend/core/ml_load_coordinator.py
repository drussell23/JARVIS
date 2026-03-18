"""backend/core/ml_load_coordinator.py — Process-wide ML weight-load coordinator.

Problem
-------
SentenceTransformer (and other heavy ML models) is instantiated in 14+ modules
across JARVIS.  When startup runs components in parallel — each in its own thread
with its own ``asyncio`` event loop — the concurrent weight reads push 16 GiB RAM
over the OOM-kill threshold.

``asyncio.Semaphore`` / ``asyncio.Lock`` cannot coordinate across separate event
loops.  This module provides a ``threading.Semaphore`` that serialises ML weight
loads at the OS-thread level, regardless of which event loop (or no loop at all)
is calling.

Usage (async context)
---------------------
::

    from backend.core.ml_load_coordinator import ml_weight_load_context

    async def _load(self) -> None:
        async with ml_weight_load_context("my_component"):
            self._model = SentenceTransformer("all-MiniLM-L6-v2")

Usage (sync context)
--------------------
::

    from backend.core.ml_load_coordinator import ml_weight_load_sync

    with ml_weight_load_sync("my_component"):
        model = SentenceTransformer("all-MiniLM-L6-v2")

Tuning
------
Set ``JARVIS_ML_LOAD_CONCURRENCY`` (default ``1``) to allow more than one
concurrent weight load when RAM allows.  Set ``JARVIS_EMBEDDER_MIN_FREE_MIB``
(default ``800``) to control the free-RAM pre-flight threshold.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from typing import AsyncIterator, Iterator, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env-tunable, frozen at module import time)
# ---------------------------------------------------------------------------

#: Maximum concurrent ML weight-loads allowed across the entire process.
_MAX_CONCURRENT: int = int(os.environ.get("JARVIS_ML_LOAD_CONCURRENCY", "1"))

#: Minimum free RAM (MiB) before any weight-load is allowed to proceed.
#: Callers that cannot satisfy this threshold log a warning and skip loading.
MIN_FREE_MIB: float = float(os.environ.get("JARVIS_EMBEDDER_MIN_FREE_MIB", "800"))

#: Seconds to wait for the semaphore before giving up.
_LOCK_TIMEOUT_S: float = 120.0

# ---------------------------------------------------------------------------
# Process-wide semaphore
# ---------------------------------------------------------------------------

#: The single process-wide threading.Semaphore.  Created once at module import.
#: threading.Semaphore is safe to acquire/release from any thread and does not
#: bind to an event loop, so it coordinates across all parallel startup threads.
ML_WEIGHT_LOAD_LOCK: threading.Semaphore = threading.Semaphore(_MAX_CONCURRENT)


# ---------------------------------------------------------------------------
# RAM measurement helper
# ---------------------------------------------------------------------------

def available_ram_mib() -> float:
    """Return currently available RAM in MiB (synchronous, no blocking I/O).

    Uses psutil when available (most accurate).  Falls back to a safe sentinel
    value so the caller can still proceed if psutil is not installed.
    """
    try:
        import psutil  # type: ignore[import]
        return psutil.virtual_memory().available / (1024.0 * 1024.0)
    except Exception:
        return 4096.0  # Assume 4 GiB free when measurement fails.


def _check_ram(component: str, required_mib: float = 0.0) -> bool:
    """Return True if there is sufficient free RAM to proceed.

    Logs a warning and returns False if ``available - required < MIN_FREE_MIB``.
    """
    free = available_ram_mib()
    headroom = free - required_mib
    if headroom < MIN_FREE_MIB:
        logger.warning(
            "[MLLoadCoordinator] '%s': insufficient RAM — "
            "free=%.0f MiB, required=%.0f MiB, headroom=%.0f MiB < threshold %.0f MiB. "
            "Skipping weight load.",
            component, free, required_mib, headroom, MIN_FREE_MIB,
        )
        return False
    logger.debug(
        "[MLLoadCoordinator] '%s': RAM ok — free=%.0f MiB, headroom=%.0f MiB",
        component, free, headroom,
    )
    return True


# ---------------------------------------------------------------------------
# Sync context manager
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def ml_weight_load_sync(
    component: str,
    required_mib: float = 0.0,
    skip_on_low_ram: bool = True,
) -> Iterator[bool]:
    """Serialise a synchronous ML weight load.

    Yields ``True`` if the load should proceed, ``False`` if it was skipped
    due to low RAM (only possible when ``skip_on_low_ram=True``).

    The semaphore is acquired before the body and released in the ``finally``
    block, ensuring no slot is leaked even if the body raises.

    Parameters
    ----------
    component:
        Human-readable name logged in all messages.
    required_mib:
        Expected peak RAM for this load (used in the headroom check).
    skip_on_low_ram:
        When True, skips the semaphore acquire and yields False instead of
        blocking if RAM is below ``MIN_FREE_MIB``.
    """
    if skip_on_low_ram and not _check_ram(component, required_mib):
        yield False
        return

    acquired = ML_WEIGHT_LOAD_LOCK.acquire(timeout=_LOCK_TIMEOUT_S)
    if not acquired:
        logger.warning(
            "[MLLoadCoordinator] '%s': timed out waiting for weight-load slot after %.0fs",
            component, _LOCK_TIMEOUT_S,
        )
        yield False
        return

    logger.debug("[MLLoadCoordinator] '%s': acquired weight-load slot", component)
    try:
        yield True
    finally:
        ML_WEIGHT_LOAD_LOCK.release()
        logger.debug("[MLLoadCoordinator] '%s': released weight-load slot", component)


# ---------------------------------------------------------------------------
# Async context manager (uses executor for the blocking acquire)
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def ml_weight_load_context(
    component: str,
    required_mib: float = 0.0,
    skip_on_low_ram: bool = True,
) -> AsyncIterator[bool]:
    """Async-friendly wrapper around :func:`ml_weight_load_sync`.

    The ``threading.Semaphore.acquire(timeout=...)`` call blocks the thread.
    To avoid blocking the event loop, the acquire is offloaded to the default
    executor (``loop.run_in_executor(None, ...)``) so other coroutines remain
    schedulable during the wait.

    Yields ``True`` if the body should run the weight load, ``False`` if it
    should skip.

    Example::

        async with ml_weight_load_context("knowledge_graph_embedder") as ok:
            if ok:
                self._embedder = await loop.run_in_executor(
                    None, lambda: SentenceTransformer(model_name)
                )
    """
    if skip_on_low_ram and not _check_ram(component, required_mib):
        yield False
        return

    loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        pass

    if loop is not None and loop.is_running():
        acquired = await loop.run_in_executor(
            None,
            lambda: ML_WEIGHT_LOAD_LOCK.acquire(timeout=_LOCK_TIMEOUT_S),
        )
    else:
        # Fallback: running outside an event loop (e.g. in a plain thread)
        acquired = ML_WEIGHT_LOAD_LOCK.acquire(timeout=_LOCK_TIMEOUT_S)

    if not acquired:
        logger.warning(
            "[MLLoadCoordinator] '%s': timed out waiting for weight-load slot after %.0fs",
            component, _LOCK_TIMEOUT_S,
        )
        yield False
        return

    logger.debug("[MLLoadCoordinator] '%s': acquired weight-load slot", component)
    try:
        yield True
    finally:
        ML_WEIGHT_LOAD_LOCK.release()
        logger.debug("[MLLoadCoordinator] '%s': released weight-load slot", component)
