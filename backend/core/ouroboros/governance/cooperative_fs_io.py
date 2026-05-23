"""Cooperative Filesystem I/O Substrate — Slice 12U Phase 1.

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
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional, Set

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
# Public surface
# ============================================================================


__all__ = [
    "COOPERATIVE_FS_IO_ENABLED_ENV_VAR",
    "cooperative_fs_io_enabled",
    "iter_files_cooperative",
    "read_text_offloaded",
]
