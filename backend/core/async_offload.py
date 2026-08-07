"""
Blocking work does not belong on the event loop — including ``import``.

Measured 2026-08-06 from ``~/Library/Logs/JARVIS/loop-stalls.log``, five
StallSampler dumps taken while the loop was provably wedged::

    dump 2 — 12.38s  main thread in ``importlib._bootstrap._lock_unlock_module``
                     networkx/algorithms/regular.py <module>
                     <- oracle.py:73 <- governed_loop_service.py:89
                     <- hud_governance_boot.py:76 <- main.py parallel_lifespan

    dump 1 —  2.34s  main thread in ``_get_module_lock.__enter__``
                     <- semantic_guardian.py:1608 <module>

    dump 4 —  2.06s  main thread in ``_path_stat`` under ``find_spec``
                     <- module_discovery.py:255

The loop was not *doing* that work. In dump 2 it was **waiting on a module
lock another thread already held** — the boot deliberately pushes heavy init
onto background threads ("Debug tasks deferred to background for fast
startup"), and one of those threads was midway through the scipy/sklearn
graph. Dump 1 caught four threads inside an import simultaneously.

So two optimizations were fighting each other over CPython's per-module import
locks: "defer heavy init to a thread" and "lazy-import inside the coroutine."
The loop lost. A module-lock wait runs no bytecode, holds the GIL region in C,
and cannot be interrupted — ``async def`` around it buys nothing, and the loop
is simply gone for the duration. Availability measured 34%, worst stall 16.33s.

Everything downstream followed from that, not from its own defect:

    [LearningDB] SQLite-first init timed out (15s) — the local path alone did
    not finish. Cloud SQL was NOT the blocker; look at event-loop starvation.
    [CapabilityRouter] 'unlock_screen' NOT authorised (I'm still loading my
    voice recognition — give me a moment and ask again.)
    [HUD] dropped a reply that waited 14.1s for the voice

This module is the one seam that keeps that work off the loop.

WHY A DEDICATED EXECUTOR AND NOT ``asyncio.to_thread``
------------------------------------------------------
``to_thread`` is already the house idiom (200+ call sites) and stays correct
for ordinary blocking calls. It is the wrong tool for *imports*, twice over:

1. It runs on asyncio's default executor, shared with every other blocking
   call in the process and sized for short work. A 12s import parked there
   starves unrelated callers.
2. It does not address the actual cost. Moving an import to *some* thread
   still leaves N threads racing for the same module locks. Funnelling every
   deferred import through **one** worker serializes the graph, so the lock
   contention that turned ~2s of work into a 12.38s wedge cannot form. The
   imports are not slower for being serialized — they were already serialized
   by the locks, just with the loop thread queued in among them.

Imports and ordinary blocking calls get separate pools for the same reason: a
long import must not delay a short secret fetch, and neither may borrow the
default executor the rest of the codebase depends on.

FAIL-OPEN, ALWAYS
-----------------
Every failure path here degrades to doing the work inline — exactly the
behaviour that existed before this module. A helper whose job is to protect
boot must never be the reason boot fails.
"""

from __future__ import annotations

import asyncio
import atexit
import importlib
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# ── Environment surface (no hardcoded policy) ────────────────────────────────
ENV_ENABLED = "JARVIS_ASYNC_OFFLOAD_ENABLED"
ENV_IMPORT_WORKERS = "JARVIS_OFFLOAD_IMPORT_WORKERS"
ENV_CALL_WORKERS = "JARVIS_OFFLOAD_CALL_WORKERS"
ENV_SLOW_MS = "JARVIS_OFFLOAD_SLOW_MS"

_DEFAULT_IMPORT_WORKERS = 1     # serialization is the point — see module docstring
_DEFAULT_CALL_WORKERS = 4
_DEFAULT_SLOW_MS = 250.0

_POOL_LOCK = threading.Lock()
_IMPORT_POOL: Optional[ThreadPoolExecutor] = None
_CALL_POOL: Optional[ThreadPoolExecutor] = None

# Set on threads owned by this module. A worker that re-enters must do the work
# inline: with a single import worker, re-dispatching onto our own pool and
# waiting for it is a self-deadlock.
_local = threading.local()


def _flag(env: os._Environ | dict, name: str, default: bool = True) -> bool:
    """Only an explicit disabling value turns a protection off."""
    raw = str(env.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _positive_int(name: str, default: int) -> int:
    """A malformed knob must not size a pool at zero and hang every caller."""
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _slow_threshold_ms() -> float:
    try:
        value = float(str(os.environ.get(ENV_SLOW_MS, "")).strip())
    except (TypeError, ValueError):
        return _DEFAULT_SLOW_MS
    return value if value > 0 else _DEFAULT_SLOW_MS


def offload_enabled() -> bool:
    """Master switch. Off ⇒ every call here degrades to inline execution."""
    return _flag(os.environ, ENV_ENABLED, default=True)


# ── Pools ────────────────────────────────────────────────────────────────────
def _mark_owned() -> None:
    _local.owned = True


def _on_offload_thread() -> bool:
    return getattr(_local, "owned", False)


def _get_pool(kind: str) -> Optional[ThreadPoolExecutor]:
    """
    Lazily build a pool. Returns None if one cannot be created, which routes the
    caller to the inline path rather than raising.
    """
    global _IMPORT_POOL, _CALL_POOL

    existing = _IMPORT_POOL if kind == "import" else _CALL_POOL
    if existing is not None:
        return existing

    with _POOL_LOCK:
        existing = _IMPORT_POOL if kind == "import" else _CALL_POOL
        if existing is not None:
            return existing
        try:
            if kind == "import":
                pool = ThreadPoolExecutor(
                    max_workers=_positive_int(ENV_IMPORT_WORKERS, _DEFAULT_IMPORT_WORKERS),
                    thread_name_prefix="jarvis-import",
                    initializer=_mark_owned,
                )
                _IMPORT_POOL = pool
            else:
                pool = ThreadPoolExecutor(
                    max_workers=_positive_int(ENV_CALL_WORKERS, _DEFAULT_CALL_WORKERS),
                    thread_name_prefix="jarvis-offload",
                    initializer=_mark_owned,
                )
                _CALL_POOL = pool
            return pool
        except Exception as exc:  # noqa: BLE001 - thread exhaustion, RLIMIT
            logger.warning(
                "[AsyncOffload] could not create %s pool (%s) — running inline; "
                "the loop may stall on this work",
                kind, exc,
            )
            return None


def _shutdown_pools(wait: bool = False) -> None:
    global _IMPORT_POOL, _CALL_POOL
    with _POOL_LOCK:
        pools = [p for p in (_IMPORT_POOL, _CALL_POOL) if p is not None]
        _IMPORT_POOL = None
        _CALL_POOL = None
    for pool in pools:
        try:
            pool.shutdown(wait=wait)
        except Exception:  # noqa: BLE001 - interpreter teardown
            pass


def _reset_after_fork() -> None:
    """
    A forked child inherits pool objects whose worker threads did not survive.
    Submitting to one blocks forever. Drop them; the next call rebuilds.
    """
    global _IMPORT_POOL, _CALL_POOL
    _IMPORT_POOL = None
    _CALL_POOL = None
    try:
        _local.owned = False
    except Exception:  # noqa: BLE001
        pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)

atexit.register(_shutdown_pools, False)


# ── Import ───────────────────────────────────────────────────────────────────
def _extract(module: Any, names: Tuple[str, ...]) -> Any:
    """Mirror ``from X import a, b`` — module, one attribute, or a tuple."""
    if not names:
        return module
    if len(names) == 1:
        return getattr(module, names[0])
    return tuple(getattr(module, n) for n in names)


def _already_usable(dotted: str) -> Any:
    """
    Fast path for a module that is fully imported.

    A module present in ``sys.modules`` may still be *mid-initialization* in
    another thread, in which case its namespace is incomplete and returning it
    hands out a half-built module. ``__spec__._initializing`` is exactly the
    flag the import system uses to answer that, and is why a plain
    ``sys.modules`` check is not sufficient.
    """
    module = sys.modules.get(dotted)
    if module is None:
        return None
    spec = getattr(module, "__spec__", None)
    if spec is not None and getattr(spec, "_initializing", False):
        return None
    return module


def _timed_import(dotted: str) -> Any:
    started = time.perf_counter()
    module = importlib.import_module(dotted)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= _slow_threshold_ms():
        logger.warning(
            "[AsyncOffload] import %s took %.0fms off-loop — "
            "the loop stayed responsive for it",
            dotted, elapsed_ms,
        )
    return module


async def import_off_loop(dotted: str, *names: str) -> Any:
    """
    ``from <dotted> import <names>`` without blocking the event loop.

    Returns the module when no names are given, the attribute for one name, or
    a tuple for several — so a call site converts without changing its shape.

    Resolution order, cheapest first:
      1. already imported and fully initialized → return inline, no thread hop
      2. offload disabled, no pool, or already on an offload thread → inline
      3. otherwise → the serialized import worker, awaited
    """
    ready = _already_usable(dotted)
    if ready is not None:
        return _extract(ready, names)

    if not offload_enabled() or _on_offload_thread():
        return _extract(_timed_import(dotted), names)

    pool = _get_pool("import")
    if pool is None:
        return _extract(_timed_import(dotted), names)

    loop = asyncio.get_running_loop()
    try:
        module = await loop.run_in_executor(pool, _timed_import, dotted)
    except RuntimeError:
        # Pool shut down underneath us (interpreter teardown, fork). The work
        # still has to happen; correctness outranks the loop here.
        module = _timed_import(dotted)
    return _extract(module, names)


# ── Arbitrary blocking calls ─────────────────────────────────────────────────
async def call_off_loop(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """
    Run a blocking synchronous callable without blocking the event loop.

    For constructors and clients that do real I/O in ``__init__`` — the
    measured case being ``SecretManagerServiceClient()``, which reaches
    ``google.auth`` and shells out to ``gcloud`` via ``subprocess.check_output``
    with no timeout (dump 3, 4.40s on the loop).

    Uses a pool separate from asyncio's default executor so this can never
    contend with the 200+ ``to_thread`` call sites already in the codebase.
    """
    if not offload_enabled() or _on_offload_thread():
        return fn(*args, **kwargs)

    pool = _get_pool("call")
    if pool is None:
        return fn(*args, **kwargs)

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(pool, lambda: fn(*args, **kwargs))
    except RuntimeError:
        return fn(*args, **kwargs)


__all__ = [
    "ENV_ENABLED",
    "ENV_IMPORT_WORKERS",
    "ENV_CALL_WORKERS",
    "ENV_SLOW_MS",
    "call_off_loop",
    "import_off_loop",
    "offload_enabled",
]
