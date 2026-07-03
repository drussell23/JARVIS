"""Cooperative Filesystem I/O Substrate — Slice 12U Phase 1
(+ Unified Offload Substrate Phase 1).

bt-2026-05-23-184213's ``LoopDeadman`` tombstone (Slice 12T Part 1)
provided unambiguous attribution for the wedge that survived Slices
12N → 12T: ``predictive_engine._fragility`` calling
``self._root.rglob("*.py")`` followed by ``py.read_text()`` directly
on the asyncio main loop. Three consecutive soaks (``171810`` /
``180315`` / ``184213``) all wedged on this exact pattern — synchronous
filesystem traversal + per-file Python I/O on the loop, holding the
GIL through the entire scan.

Slice 12U eradicates this **entire class** of vulnerability — not by
patching the one offender, but by providing a single canonical
substrate that every subsystem can compose. The substrate fully
leverages every primitive we already built:

* ``operation_advisor._get_advisor_blast_executor`` (Task #88f) — the
  dedicated, bounded ``advisor-blast`` ThreadPoolExecutor that
  isolates FS I/O from the contested default pool (Slice 12T Part 3
  restored this contract after Slice 12S accidentally broke it).

* ``event_loop_governance.cooperative_yield_every_n_async`` (Task #102)
  — the canonical primitive that inserts ``asyncio.sleep(0)`` every N
  items in async iteration so the heartbeat coroutine + Claude SDK
  stream consumer get scheduling slots.

* ``bounded_walker.iter_bounded_files`` — the bounded directory
  walker with skip-dirs / max-scanned / timeout-s guards.

This module is the **single source of truth** for cooperative
filesystem I/O across the active asyncio loop. NO new threading
mechanism, NO new bounding primitive — pure composition.

# Use cases (active on-loop offenders)

* ``predictive_engine._fragility``: traverses repo for ``*.py`` files,
  reads each one, regex-extracts imports. Proven wedge.

* Future: any periodic background coroutine that does ``rglob`` +
  per-file ``read_text`` should compose this substrate instead of
  rolling its own off-loop wrapper. The audit/exorcism in Phase 2
  immunizes the substrate against future I/O wedges (per operator
  binding 2026-05-23).

# Master switch

``JARVIS_COOPERATIVE_FS_IO_ENABLED`` (BOOL/SAFETY, default TRUE) —
gates the cooperative path. When ``false``, callers fall back to
their pre-Slice-12U synchronous behavior verbatim (byte-identical
rollback). Composes the existing Task #102 master switch
(``JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED``) — when the underlying
governance is disabled, this substrate naturally degrades too.

# Architectural invariants

* Per-file reads dispatch through the DEDICATED ``advisor-blast``
  executor — NEVER ``asyncio.to_thread`` (which targets the
  contested default pool — the Slice 12S antipattern).

* Iteration uses ``cooperative_yield_every_n_async`` — NOT custom
  sleep cadences. Cadence comes from
  ``JARVIS_EVENT_LOOP_YIELD_EVERY_N`` (default 64). Single source
  of truth.

* Bounded by default — ``iter_files_cooperative`` honors the
  ``bounded_walker`` budget knobs (max_scanned / timeout_s /
  skip_dirs) so even unbounded callers get protection from
  pathological trees.

* NEVER raises into the caller's coroutine. Read failures return
  ``None`` (same contract as ``bounded_walker.bounded_read_text``);
  iteration errors terminate the iterator cleanly.

# Unified offload substrate (this extension)

An audit of the sync-FS-on-loop bug class (``docs/.../fs-io-audit.md``)
found THREE divergent ad-hoc offload mechanisms had grown up around
this substrate instead of composing it: ``cooperative_fs_io``'s own
``advisor-blast`` ThreadPoolExecutor, ``stdlib_self_health_oracle.py``'s
own ``run_in_executor(None, ...)`` (the contested default pool), and
``posture_observer.py``'s own dedicated 2-worker executor. This module
now exposes the single delegation primitive those three (and every
future CPU-after-scan caller — SHA-256 hashing, AST parsing, embedding
inference) should converge on:

* :func:`offload` — routes IO-bound work (``cpu_bound=False``, the
  default) through the EXISTING ``advisor-blast`` ThreadPoolExecutor
  (same pool ``read_text_offloaded`` uses — no second thread pool),
  and pure-Python CPU-after-scan work (``cpu_bound=True``) through a
  lazily-created, bounded ``ProcessPoolExecutor`` (threads don't
  release the GIL for pure-Python compute; a process does).

* :func:`walk_for_filename_offloaded` — offloaded equivalent of
  ``source_crawlers._walk_for_filename``, preserving its in-place
  ``dirnames[:]`` prune (structural descent-control, not post-hoc
  filtering) and returning whatever was collected on a deadline
  overrun.

## Contract for ``offload()``

* **Never-raise for runtime faults.** Exceptions raised BY the
  delegated ``fn`` (``PermissionError``, ``OSError``,
  ``FileNotFoundError``, a symlink-loop ``RecursionError``, ...) are
  caught inside the worker and returned to the caller as an
  :class:`OffloadError` — they do NOT propagate as a raised exception.
  Callers check ``isinstance(result, OffloadError)`` (or the
  :func:`is_offload_error` helper). This mirrors
  ``read_text_offloaded``'s "return ``None`` on error" contract, just
  generalized to an arbitrary payload-agnostic ``fn``.

* **Loud failure for pickling faults.** ``cpu_bound=True`` requires
  ``fn``/``args``/``kwargs`` to be picklable (``ProcessPoolExecutor``
  IPC marshals them across the process boundary). This is a CALLER
  bug, not a runtime FS fault, so it is validated eagerly — BEFORE
  the process-pool submission — and raises :class:`OffloadPicklingError`
  with a clear message identifying the offending object, instead of
  letting a cryptic ``PicklingError``/``BrokenProcessPool`` surface
  deep inside the pool's IPC layer.

* **Module-level targets only for ``cpu_bound=True``.** macOS Python
  3.11+ defaults the process-pool start method to ``'spawn'`` (no
  ``fork()`` COW inheritance of closures) — the target callable must
  be picklable by reference (a module-level function), NOT a closure,
  lambda, or bound method on a non-picklable object. Closures MUST use
  the thread path (``cpu_bound=False``) instead; see
  :func:`offload`'s docstring.

## Bounded process pool

``JARVIS_FS_PROCESS_POOL_WORKERS`` (default
``min(2, os.cpu_count() // 2 or 1)``) — lazily created on the FIRST
``cpu_bound=True`` call (never at import time — avoids paying fork/
spawn cost for callers that only ever use the thread path), module-
level singleton, ``atexit``-registered clean shutdown (also exposed
as :func:`shutdown_fs_process_pool` for explicit lifecycle callers
such as tests / graceful service shutdown).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import pickle
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    FrozenSet,
    List,
    Optional,
    Set,
)

logger = logging.getLogger("Ouroboros.CooperativeFSIO")


# ============================================================================
# Master switch
# ============================================================================


COOPERATIVE_FS_IO_ENABLED_ENV_VAR: str = (
    "JARVIS_COOPERATIVE_FS_IO_ENABLED"
)


def cooperative_fs_io_enabled() -> bool:
    """Master flag for Slice 12U cooperative FS I/O.

    Default TRUE — the cooperative path is the production default
    post-Slice-12U. Set to ``false`` / ``0`` / ``no`` / ``off`` to
    revert callers to byte-identical pre-Slice-12U synchronous
    behavior (e.g. for emergency rollback).
    """
    raw = os.environ.get(
        COOPERATIVE_FS_IO_ENABLED_ENV_VAR, "true",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ============================================================================
# Cooperative async I/O primitives
# ============================================================================


async def read_text_offloaded(
    path: Path,
    *,
    max_bytes: Optional[int] = None,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> Optional[str]:
    """Cooperative async wrapper around ``Path.read_text``.

    Dispatches the read to the DEDICATED ``advisor-blast``
    ThreadPoolExecutor (NOT ``asyncio.to_thread`` — the Slice 12S
    antipattern that contested the default pool with sensors + Oracle
    + DreamEngine). Returns ``None`` on any error — same contract as
    :func:`bounded_walker.bounded_read_text`; the caller must handle
    None explicitly. NEVER raises.

    When ``max_bytes`` is provided, the read is bounded (composes
    :func:`bounded_walker.bounded_read_text`). When ``None``, reads
    the whole file via ``Path.read_text`` — caller's responsibility
    to ensure the path is not pathological.

    When the master switch is FALSE, falls back to a synchronous
    in-line ``path.read_text()`` for byte-identical pre-Slice-12U
    rollback. Caller still ``await``s — the await resolves
    immediately with the synchronous result.
    """
    if not cooperative_fs_io_enabled():
        # Legacy rollback path — synchronous read in caller's
        # coroutine (preserves pre-Slice-12U semantics).
        try:
            if max_bytes is not None:
                from backend.core.ouroboros.governance.bounded_walker import (  # noqa: E501
                    bounded_read_text,
                )
                return bounded_read_text(path, max_bytes=max_bytes)
            return path.read_text(encoding=encoding, errors=errors)
        except Exception:  # noqa: BLE001
            return None

    # Cooperative path — dispatch to the dedicated executor.
    try:
        from backend.core.ouroboros.governance.operation_advisor import (  # noqa: E501
            _get_advisor_blast_executor,
        )
        loop = asyncio.get_running_loop()
        executor = _get_advisor_blast_executor()
        return await loop.run_in_executor(
            executor,
            _read_text_worker,
            str(path), max_bytes, encoding, errors,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CooperativeFSIO] read_text_offloaded(%s) failed",
            path, exc_info=True,
        )
        return None


def _read_text_worker(
    path_str: str,
    max_bytes: Optional[int],
    encoding: str,
    errors: str,
) -> Optional[str]:
    """Module-level worker dispatched to the executor thread.

    Lifted out of ``read_text_offloaded`` so the executor's worker
    doesn't capture any caller-local state, mirroring the
    :func:`operation_advisor._read_bounded_text_for_blast` pattern
    from Slice 12T Part 3. NEVER raises — returns ``None`` on any
    error.
    """
    try:
        if max_bytes is not None:
            from backend.core.ouroboros.governance.bounded_walker import (  # noqa: E501
                bounded_read_text,
            )
            return bounded_read_text(Path(path_str), max_bytes=max_bytes)
        return Path(path_str).read_text(
            encoding=encoding, errors=errors,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


async def iter_files_cooperative(
    root: Path,
    *,
    pattern: str = "*",
    max_scanned: Optional[int] = None,
    timeout_s: Optional[float] = None,
    skip_dirs: Optional[Set[str]] = None,
    yield_every_n: Optional[int] = None,
) -> AsyncIterator[str]:
    """Cooperative async file iterator.

    Walks ``root`` for files matching ``pattern`` (default ``*`` —
    all files; callers typically pass ``*.py``), composing
    :func:`bounded_walker.iter_bounded_files` for the bounded walk
    + :func:`event_loop_governance.cooperative_yield_every_n_async`
    for the yield cadence. Per-walk-step yield is automatic; the
    loop's heartbeat coroutine gets scheduling slots throughout.

    ``max_scanned`` / ``timeout_s`` default to the Advisor blast
    knobs (``JARVIS_BLAST_RADIUS_MAX_SCANNED`` /
    ``JARVIS_BLAST_RADIUS_TIMEOUT_S``) when ``None`` — same budget
    contract callers like ``predictive_engine`` previously had no
    way to opt into.

    ``yield_every_n`` defaults to ``JARVIS_EVENT_LOOP_YIELD_EVERY_N``
    (typically 64). Operators do NOT tune Slice 12U separately;
    reusing the existing knob preserves single-source-of-truth for
    loop-yield rhythm.

    ``skip_dirs`` defaults to the canonical
    ``bounded_walker.default_skip_dirs()`` (``.git``, ``node_modules``,
    ``__pycache__``, ``.venv``, etc.). Pass an explicit set to
    override; pass ``set()`` to disable skipping (rarely correct).

    Yields filename strings matching ``pattern`` — caller filters by
    extension / domain. NEVER raises; iteration terminates cleanly
    on any unexpected error.

    Master switch FALSE: degrades to a non-cooperative async wrapper
    over the bounded walker — no yield injection, no behavioral
    difference for callers that already work without yields.
    """
    from backend.core.ouroboros.governance.bounded_walker import (  # noqa: E501
        blast_radius_max_scanned,
        blast_radius_timeout_s,
        default_skip_dirs,
        iter_bounded_files,
    )
    from backend.core.ouroboros.governance.event_loop_governance import (  # noqa: E501
        cooperative_yield_every_n_async,
    )

    _max_scanned = (
        max_scanned if max_scanned is not None
        else blast_radius_max_scanned()
    )
    _timeout_s = (
        timeout_s if timeout_s is not None
        else blast_radius_timeout_s()
    )
    _skip = (
        skip_dirs if skip_dirs is not None
        else default_skip_dirs()
    )

    # Build the bounded iterator (synchronous generator — the
    # cooperative_yield_every_n_async wrapper handles the yield
    # cadence per item).
    bounded_iter = iter_bounded_files(
        root,
        max_scanned=_max_scanned,
        timeout_s=_timeout_s,
        skip_dirs=_skip,
    )

    if not cooperative_fs_io_enabled():
        # Legacy rollback — pure async wrapper, no yield injection.
        try:
            for path_str in bounded_iter:
                if pattern == "*" or _matches_pattern(path_str, pattern):
                    yield path_str
        except Exception:  # noqa: BLE001
            logger.debug(
                "[CooperativeFSIO] iter_files_cooperative(%s) failed",
                root, exc_info=True,
            )
        return

    # Cooperative path — yield to the loop every N items.
    kwargs: dict = {}
    if yield_every_n is not None:
        kwargs["every_n"] = yield_every_n

    try:
        async for path_str in cooperative_yield_every_n_async(
            bounded_iter, **kwargs,
        ):
            if pattern == "*" or _matches_pattern(path_str, pattern):
                yield path_str
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CooperativeFSIO] iter_files_cooperative(%s) async "
            "iteration failed",
            root, exc_info=True,
        )


def _matches_pattern(path_str: str, pattern: str) -> bool:
    """Lightweight pattern matcher.

    Currently supports ``*.<ext>`` style suffixes (the only form
    callers in the active loop use — ``*.py`` for
    ``predictive_engine``, ``*.md`` for doc scans). Pure-stdlib —
    no fnmatch import to keep the iteration hot path lean.
    """
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".py"
        return path_str.endswith(suffix)
    if pattern == "*":
        return True
    # Fallback: full fnmatch (cold path — only when an exotic
    # pattern is passed).
    try:
        import fnmatch
        return fnmatch.fnmatch(path_str, pattern)
    except Exception:  # noqa: BLE001
        return False


# ============================================================================
# Unified offload substrate — offload() + bounded process pool
# ============================================================================


class OffloadPicklingError(TypeError):
    """A ``cpu_bound=True`` :func:`offload` target/args/kwargs are not
    picklable.

    Raised EAGERLY — before the ``ProcessPoolExecutor`` is ever
    touched — so the failure is a clear, immediate ``TypeError`` with
    the offending object named, not a ``PicklingError`` /
    ``BrokenProcessPool`` surfacing minutes later from inside the
    pool's IPC layer. This is a caller/programming-error signal (fix
    the call site), distinct from :class:`OffloadError` (a runtime
    fault INSIDE the delegated function, which is fail-soft).
    """


@dataclass(frozen=True)
class OffloadError:
    """Structured fail-soft result for a failed :func:`offload` call.

    ``offload()`` NEVER lets the delegated ``fn``'s runtime exceptions
    (``PermissionError``, ``OSError``, ``FileNotFoundError``, a
    symlink-loop ``RecursionError``, ...) propagate into the caller's
    coroutine — the worker catches them and returns this dataclass
    instead. Callers that need the raw failure detail read
    ``exc_type``/``message``; callers that just need a boolean use
    :func:`is_offload_error`.
    """

    fn_name: str
    exc_type: str
    message: str
    cpu_bound: bool


def is_offload_error(result: Any) -> bool:
    """``True`` iff ``result`` is a fail-soft :class:`OffloadError`
    returned by :func:`offload` (as opposed to the delegated fn's
    successful return value)."""
    return isinstance(result, OffloadError)


_FS_PROCESS_POOL_WORKERS_ENV_VAR = "JARVIS_FS_PROCESS_POOL_WORKERS"

_FS_PROCESS_POOL: "Optional[object]" = None
_FS_PROCESS_POOL_LOCK = threading.Lock()


def _default_fs_process_pool_workers() -> int:
    """``min(2, os.cpu_count() // 2 or 1)`` — conservative default so
    the process pool never competes hard with the main workload for
    cores on small machines."""
    cpu = os.cpu_count() or 1
    half = cpu // 2 or 1
    return max(1, min(2, half))


def fs_process_pool_workers() -> int:
    """``JARVIS_FS_PROCESS_POOL_WORKERS`` — worker count for the
    lazily-created bounded FS process pool. Falls back to
    :func:`_default_fs_process_pool_workers` on unset/invalid/
    non-positive input. NEVER raises."""
    raw = os.environ.get(_FS_PROCESS_POOL_WORKERS_ENV_VAR, "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return _default_fs_process_pool_workers()


def _get_fs_process_pool():
    """Lazy-init singleton bounded ``ProcessPoolExecutor`` for
    ``cpu_bound=True`` offload work.

    Created on the FIRST ``cpu_bound=True`` call — NEVER at import
    time (avoids paying fork/spawn cost for the majority of callers
    that only ever use the thread path). Registers an ``atexit``
    shutdown hook so worker processes don't outlive the parent.
    Thread-safe double-checked-locking singleton (mirrors
    ``operation_advisor._get_advisor_blast_executor``).
    """
    global _FS_PROCESS_POOL
    if _FS_PROCESS_POOL is None:
        with _FS_PROCESS_POOL_LOCK:
            if _FS_PROCESS_POOL is None:
                import multiprocessing as _mp
                from concurrent.futures import ProcessPoolExecutor

                workers = fs_process_pool_workers()
                try:
                    # macOS py3.11+ already defaults to 'spawn'; pin
                    # it explicitly so behavior is identical on
                    # platforms where 'fork' is still the default
                    # (Linux) — spawn is required for closures to
                    # fail loudly via OffloadPicklingError instead of
                    # silently working under fork's COW semantics
                    # and then breaking the moment we deploy to a
                    # spawn-only platform.
                    ctx = _mp.get_context("spawn")
                except (ValueError, RuntimeError):  # noqa: BLE001
                    ctx = None
                _FS_PROCESS_POOL = ProcessPoolExecutor(
                    max_workers=workers,
                    mp_context=ctx,
                )
                atexit.register(shutdown_fs_process_pool)
                logger.info(
                    "[CooperativeFSIO] lazily created bounded FS "
                    "process pool (max_workers=%d, start_method=spawn)",
                    workers,
                )
    return _FS_PROCESS_POOL


def shutdown_fs_process_pool() -> None:
    """Clean shutdown hook for the FS process pool.

    Idempotent — safe to call multiple times (e.g. once from
    ``atexit`` and once from an explicit test/service teardown).
    NEVER raises.
    """
    global _FS_PROCESS_POOL
    pool = _FS_PROCESS_POOL
    if pool is None:
        return
    _FS_PROCESS_POOL = None
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        # cancel_futures added in py3.9; defensive fallback for any
        # backport oddities.
        try:
            pool.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _validate_picklable_for_process_pool(
    fn: Callable[..., Any],
    args: tuple,
    kwargs: dict,
) -> None:
    """Eagerly validate ``fn``/``args``/``kwargs`` pickle cleanly.

    Raises :class:`OffloadPicklingError` IMMEDIATELY (synchronously,
    before any executor submission) with a clear, actionable message
    naming the offending object — the whole point being a caller
    never has to debug a ``PicklingError`` traceback that bottoms out
    inside ``concurrent.futures.process``.
    """
    fn_label = getattr(fn, "__name__", repr(fn))
    try:
        pickle.dumps(fn)
    except Exception as exc:  # noqa: BLE001 — re-raised as typed error
        raise OffloadPicklingError(
            f"offload(cpu_bound=True): fn={fn_label!r} is not "
            f"picklable ({type(exc).__name__}: {exc}). cpu_bound "
            "offload targets MUST be module-level functions — not "
            "closures, lambdas, or bound methods on a non-picklable "
            "object. Use cpu_bound=False (the thread path) for "
            "closures instead."
        ) from exc
    try:
        pickle.dumps(args)
        pickle.dumps(kwargs)
    except Exception as exc:  # noqa: BLE001 — re-raised as typed error
        raise OffloadPicklingError(
            f"offload(cpu_bound=True): args/kwargs for fn={fn_label!r} "
            f"are not picklable ({type(exc).__name__}: {exc}). "
            "Process-pool handoff requires a fully picklable payload "
            "— pass primitives / plain dataclasses / paths-as-str, "
            "not open file handles, locks, sockets, or closures."
        ) from exc


def _offload_worker(
    fn: Callable[..., Any],
    args: tuple,
    kwargs: dict,
    cpu_bound: bool,
) -> Any:
    """Module-level trampoline dispatched into the thread OR process
    pool.

    Module-level (not a nested closure) so it is picklable for the
    ``ProcessPoolExecutor`` path. Catches ALL exceptions raised by
    ``fn`` and returns an :class:`OffloadError` instead of letting
    them propagate — the never-raise contract for runtime faults
    (``PermissionError``/``OSError``/``FileNotFoundError``/symlink-
    loop ``RecursionError``/anything else ``fn`` may raise while
    scanning a pathological tree).
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — fail-soft contract
        return OffloadError(
            fn_name=getattr(fn, "__name__", repr(fn)),
            exc_type=type(exc).__name__,
            message=str(exc),
            cpu_bound=cpu_bound,
        )


async def offload(
    fn: Callable[..., Any],
    /,
    *args: Any,
    cpu_bound: bool = False,
    **kwargs: Any,
) -> Any:
    """The single unified delegation primitive for off-loop work.

    ``cpu_bound=False`` (default) — routes ``fn`` through the
    EXISTING shared ``advisor-blast`` ``ThreadPoolExecutor`` (the same
    pool :func:`read_text_offloaded` uses — no second thread pool is
    created). Correct for IO-bound / GIL-releasing work: syscall-heavy
    directory walks, file reads, stats.

    ``cpu_bound=True`` — routes ``fn`` through a lazily-created,
    bounded ``ProcessPoolExecutor`` (:func:`_get_fs_process_pool`).
    Correct for pure-Python CPU-after-scan work — SHA-256 hashing,
    AST parsing, embedding inference — where a thread would hold the
    GIL for the whole computation and still starve the loop.

    Both paths dispatch via ``loop.run_in_executor`` — the calling
    coroutine suspends (the loop keeps servicing other tasks) until
    the executor completes.

    Payload-agnostic: makes no assumption about what ``fn`` scans,
    reads, or computes.

    **Contract** (see module docstring "Unified offload substrate"
    for the full rationale):

    * Success → returns ``fn``'s return value verbatim.
    * ``fn`` raises at runtime → caught, returned as an
      :class:`OffloadError` (never re-raised into the caller's
      coroutine). Check with :func:`is_offload_error`.
    * ``cpu_bound=True`` with a non-picklable ``fn``/``args``/
      ``kwargs`` → raises :class:`OffloadPicklingError` IMMEDIATELY,
      synchronously, before any pool submission — a loud, clear
      failure naming the offending object (a caller bug, not a
      runtime fault, so it is NOT fail-soft).
    * ``cpu_bound=True`` requires ``fn`` to be a module-level function
      (picklable by reference) — closures/lambdas must use
      ``cpu_bound=False`` instead.

    Master switch (``JARVIS_COOPERATIVE_FS_IO_ENABLED``) FALSE:
    degrades to running ``fn`` synchronously in-line via the same
    fail-soft trampoline (no executor dispatch) — byte-identical-
    shape rollback consistent with the rest of this module.
    """
    if not cooperative_fs_io_enabled():
        if cpu_bound:
            # Still validate eagerly under master-off so caller bugs
            # surface identically regardless of the flag — the
            # pickling contract is a correctness property of the
            # call site, not a behavior the master switch should
            # mask.
            _validate_picklable_for_process_pool(fn, args, kwargs)
        return _offload_worker(fn, args, kwargs, cpu_bound)

    loop = asyncio.get_running_loop()

    if cpu_bound:
        _validate_picklable_for_process_pool(fn, args, kwargs)
        try:
            executor = _get_fs_process_pool()
        except Exception as exc:  # noqa: BLE001 — pool creation fault
            logger.debug(
                "[CooperativeFSIO] offload(cpu_bound=True) failed to "
                "acquire process pool", exc_info=True,
            )
            return OffloadError(
                fn_name=getattr(fn, "__name__", repr(fn)),
                exc_type=type(exc).__name__,
                message=str(exc),
                cpu_bound=True,
            )
        return await loop.run_in_executor(
            executor, _offload_worker, fn, args, kwargs, True,
        )

    # Thread path — reuse the EXISTING advisor-blast pool (do not
    # create a second thread pool).
    from backend.core.ouroboros.governance.operation_advisor import (  # noqa: E501
        _get_advisor_blast_executor,
    )
    executor = _get_advisor_blast_executor()
    return await loop.run_in_executor(
        executor, _offload_worker, fn, args, kwargs, False,
    )


# ============================================================================
# walk_for_filename_offloaded — offloaded source_crawlers._walk_for_filename
# ============================================================================


_DEFAULT_WALK_FOR_FILENAME_MAX_WALK_S = 10.0


def _walk_for_filename_worker(
    root_str: str,
    filename: str,
    skip_dirnames: FrozenSet[str],
    max_results: Optional[int],
    max_walk_s: float,
) -> List[str]:
    """Module-level worker for :func:`walk_for_filename_offloaded`.

    Mirrors ``source_crawlers._walk_for_filename`` EXACTLY on the
    load-bearing points:

    * ``dirnames[:] = [...]`` — IN-PLACE prune of the ``os.walk``
      dirnames list, so descent into ``skip_dirnames`` subtrees is
      structurally prevented (never even ``stat``'d), not filtered
      post-hoc on the full path (the bt-2026-05-25-050449 wedge
      pattern this guards against).
    * A wall-clock ``max_walk_s`` deadline — returns whatever was
      collected so far on overrun rather than raising or hanging.

    Runs inside the (thread OR process) executor — ``os.walk``
    releases the GIL on its underlying syscalls, so even the thread
    path frees the loop. NEVER raises — returns a partial/empty list
    on any error.
    """
    matches: List[str] = []
    deadline = time.monotonic() + max_walk_s
    try:
        for dirpath, dirnames, filenames in os.walk(root_str):
            if time.monotonic() > deadline:
                logger.warning(
                    "[CooperativeFSIO] walk_for_filename_offloaded(%s, "
                    "%s) exceeded max_walk_s=%.1fs (collected %d so "
                    "far) — returning partial results",
                    root_str, filename, max_walk_s, len(matches),
                )
                break
            dirnames[:] = [
                d for d in dirnames if d not in skip_dirnames
            ]
            if filename in filenames:
                matches.append(os.path.join(dirpath, filename))
                if (
                    max_results is not None
                    and len(matches) >= max_results
                ):
                    break
    except Exception:  # noqa: BLE001 — defensive, never raise
        logger.debug(
            "[CooperativeFSIO] walk_for_filename_offloaded worker "
            "failed for root=%s filename=%s",
            root_str, filename, exc_info=True,
        )
    return sorted(matches)


async def walk_for_filename_offloaded(
    root: Path,
    filename: str,
    *,
    skip_dirnames: Optional[FrozenSet[str]] = None,
    max_results: Optional[int] = None,
    max_walk_s: float = _DEFAULT_WALK_FOR_FILENAME_MAX_WALK_S,
) -> List[Path]:
    """Offloaded equivalent of ``source_crawlers._walk_for_filename``.

    Walks ``root`` collecting paths to files named ``filename``, off
    the asyncio loop, preserving the original's in-place
    ``dirnames[:]`` prune semantics (see :func:`_walk_for_filename_worker`).

    ``skip_dirnames`` defaults to the canonical
    ``bounded_walker.default_skip_dirs()`` set (``.git``,
    ``node_modules``, ``__pycache__``, ``.venv``, ``target``, etc.) —
    pass an explicit ``frozenset`` to override; no hardcoded caller-
    specific list is baked into the substrate.

    ``max_results`` caps the number of matches collected (``None`` —
    the default — means unbounded, matching the original's
    behavior); the walk stops early once the cap is hit.

    ``max_walk_s`` bounds the walk's wall-clock duration as
    defense-in-depth against pathological trees; on overrun, returns
    whatever was collected so far (never raises, never hangs).

    Runs the ``os.walk`` in the shared ``advisor-blast`` thread pool
    — IO-bound (syscall-dominated), so a thread (not a process) is
    the correct pool: ``os.walk`` releases the GIL on ``stat``/
    ``readdir`` syscalls.

    Master switch (``JARVIS_COOPERATIVE_FS_IO_ENABLED``) FALSE:
    degrades to running the walk synchronously in-line — byte-
    identical-shape rollback.

    NEVER raises into the caller's coroutine.
    """
    from backend.core.ouroboros.governance.bounded_walker import (  # noqa: E501
        default_skip_dirs,
    )

    _skip = (
        skip_dirnames if skip_dirnames is not None
        else frozenset(default_skip_dirs())
    )

    if not cooperative_fs_io_enabled():
        result = _walk_for_filename_worker(
            str(root), filename, _skip, max_results, max_walk_s,
        )
        return [Path(p) for p in result]

    try:
        from backend.core.ouroboros.governance.operation_advisor import (  # noqa: E501
            _get_advisor_blast_executor,
        )
        loop = asyncio.get_running_loop()
        executor = _get_advisor_blast_executor()
        result = await loop.run_in_executor(
            executor,
            _walk_for_filename_worker,
            str(root), filename, _skip, max_results, max_walk_s,
        )
        return [Path(p) for p in result]
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CooperativeFSIO] walk_for_filename_offloaded(%s, %s) "
            "failed", root, filename, exc_info=True,
        )
        return []


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "COOPERATIVE_FS_IO_ENABLED_ENV_VAR",
    "cooperative_fs_io_enabled",
    "iter_files_cooperative",
    "read_text_offloaded",
    "offload",
    "walk_for_filename_offloaded",
    "OffloadError",
    "OffloadPicklingError",
    "is_offload_error",
    "fs_process_pool_workers",
    "shutdown_fs_process_pool",
]
