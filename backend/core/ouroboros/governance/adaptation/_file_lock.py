"""Phase 7.8 — cross-process file lock helper for AdaptationLedger.

Per `OUROBOROS_VENOM_PRD.md` §3.6.2 fragility-vector #3:

  > Cross-process AdaptationLedger race — `threading.RLock` only
  > serializes within-process; concurrent miners across processes
  > race. Solution: add `fcntl.flock` advisory file lock around
  > append paths. Best-effort fallback to current behavior if
  > `fcntl` unavailable (Windows).

This helper is a private substrate primitive — only `ledger.py` should
import it. Same one-way dependency rule as the rest of `adaptation/`.

## Design constraints

  * **Best-effort, never raise**. If `fcntl` is unavailable (Windows)
    OR `flock` itself raises (NFS / unsupported FS), the context
    manager degrades to a no-op rather than blocking the write.
  * **Operator kill switch**: `JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED`
    defaults TRUE (security hardening on by default — same convention
    as P7.7 Rule 7).
  * **Stdlib-only**. ``fcntl`` is conditionally imported.
  * **Lock granularity**: one lock per *file descriptor* — callers
    must hold the lock for the duration of their atomic operation
    (open → write → flush → close). The ``flock_exclusive`` helper
    wraps an already-open file handle.
  * **Shared vs exclusive**: ``flock_shared(fd)`` for read paths;
    ``flock_exclusive(fd)`` for append paths. Multiple readers can
    hold shared locks concurrently; an exclusive lock blocks all
    other lockers (shared OR exclusive).

## Default-on

`JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED` (default true — defense-in-
depth on by default; operators can disable in emergency without
disabling the ledger itself).
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


def is_flock_enabled() -> bool:
    """Per-feature kill switch —
    ``JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED``. Defaults TRUE
    (security hardening on by default; operator can disable in
    emergency without disabling the whole ledger)."""
    raw = os.environ.get("JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED")
    if raw is None:
        return True  # default-ON
    return raw.strip().lower() in _TRUTHY


def _try_import_fcntl() -> Optional[object]:
    """Return the `fcntl` module if available; else None.

    `fcntl` is POSIX-only; Windows ImportError → None → no-op
    fallback. Tested on macOS + Linux. Other platforms degrade
    silently (a log line is emitted on first attempt; no
    repeated noise)."""
    try:
        import fcntl as _fcntl
        return _fcntl
    except ImportError:
        return None


# Cache the fcntl import outcome so we log once.
_FCNTL = _try_import_fcntl()
_FCNTL_LOG_EMITTED = False


def _maybe_log_no_fcntl() -> None:
    global _FCNTL_LOG_EMITTED
    if _FCNTL_LOG_EMITTED:
        return
    _FCNTL_LOG_EMITTED = True
    logger.info(
        "[AdaptationLedger] fcntl unavailable (likely Windows) — "
        "cross-process file locking degraded to no-op",
    )


@contextlib.contextmanager
def flock_exclusive(fd: int) -> Iterator[bool]:
    """Acquire an exclusive (LOCK_EX) advisory lock on the file
    descriptor for the duration of the context.

    Yields:
        True if the lock was acquired (or no-op fallback fired);
        False if a lock attempt was made and FAILED (raised).

    NEVER raises. Best-effort:
      * fcntl unavailable → no-op + log once + yield True
      * Per-feature kill switch off → no-op + yield True
      * fcntl.flock raises (NFS, unsupported FS) → log + yield False

    Caller must hold the lock for the duration of their atomic
    operation. The lock is released automatically on context exit
    (LOCK_UN via fcntl.flock).
    """
    if not is_flock_enabled():
        yield True
        return
    if _FCNTL is None:
        _maybe_log_no_fcntl()
        yield True
        return
    try:
        _FCNTL.flock(fd, _FCNTL.LOCK_EX)  # type: ignore[attr-defined]
    except OSError as exc:
        logger.warning(
            "[AdaptationLedger] flock_exclusive failed: %s — "
            "continuing without cross-process lock", exc,
        )
        yield False
        return
    try:
        yield True
    finally:
        try:
            _FCNTL.flock(fd, _FCNTL.LOCK_UN)  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning(
                "[AdaptationLedger] flock release failed: %s "
                "(lock should auto-release on fd close)", exc,
            )


@contextlib.contextmanager
def flock_shared(fd: int) -> Iterator[bool]:
    """Acquire a shared (LOCK_SH) advisory lock on the file
    descriptor for the duration of the context.

    Multiple readers can hold shared locks concurrently; an
    exclusive lock (held by another process) blocks all shared
    lockers until released.

    Yields True (acquired or no-op fallback) / False (attempt
    failed). Same fail-open semantics as `flock_exclusive`.
    """
    if not is_flock_enabled():
        yield True
        return
    if _FCNTL is None:
        _maybe_log_no_fcntl()
        yield True
        return
    try:
        _FCNTL.flock(fd, _FCNTL.LOCK_SH)  # type: ignore[attr-defined]
    except OSError as exc:
        logger.warning(
            "[AdaptationLedger] flock_shared failed: %s — "
            "continuing without cross-process lock", exc,
        )
        yield False
        return
    try:
        yield True
    finally:
        try:
            _FCNTL.flock(fd, _FCNTL.LOCK_UN)  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning(
                "[AdaptationLedger] flock release failed: %s "
                "(lock should auto-release on fd close)", exc,
            )


def _reset_log_emitted_for_test() -> None:
    """Test-only: reset the once-log gate so multiple tests can
    exercise the fallback log path."""
    global _FCNTL_LOG_EMITTED
    _FCNTL_LOG_EMITTED = False


__all__ = [
    "flock_exclusive",
    "flock_shared",
    "is_flock_enabled",
]
